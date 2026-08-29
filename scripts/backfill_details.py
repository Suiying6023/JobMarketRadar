"""给存量数据补详情（JD、活跃度等）。

之前的批次里 detail_limit_per_query 设得太小（10），导致 1093 条里只有 90 条
抓到了详情。列表响应里的 securityId/lid 已经存进了 raw_json，所以不需要重新
搜索——直接用存量的 securityId/lid 调详情接口，比重新爬列表更省请求数。

用法：
    python scripts/backfill_details.py --config config.hangzhou.yaml --limit 150

--limit 控制这一批处理多少条缺 JD 的记录，默认 150（配合详情间隔 3-7 秒，
一批大约 10-15 分钟），方便按用户要求的“爬一批查一批”节奏分批跑，
命中风控会立刻停止（RiskControlStop 不重试）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar.collectors.boss import SEARCH_BASE, BossCollector, RiskControlStop  # noqa: E402
from job_radar.config import load_config  # noqa: E402
from job_radar.exit_guard import assert_home_exit  # noqa: E402
from job_radar.models import JobRecord, SearchFilters  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

# 投递池判定的唯一实现。这里复用它而不是另写一套，否则两份阈值会各自漂移。
from clean_local_db import should_demote  # noqa: E402


def _row_to_job(row) -> JobRecord:
    data = dict(row)
    return JobRecord(
        platform=data["platform"],
        source_job_id=data["source_job_id"],
        job_url=data["job_url"],
        company_url=data["company_url"],
        title=data["title"],
        company_name=data["company_name"],
        city=data["city"],
        district=data["district"],
        business_district=data["business_district"],
        location_text=data["location_text"],
        salary_text=data["salary_text"],
        salary_min_k=data["salary_min_k"],
        salary_max_k=data["salary_max_k"],
        salary_months=data["salary_months"],
        experience=data["experience"],
        education=data["education"],
        company_size=data["company_size"],
        financing_stage=data["financing_stage"],
        industry=data["industry"],
        recruiter_name=data["recruiter_name"],
        recruiter_title=data["recruiter_title"],
        recruiter_active_status=data["recruiter_active_status"],
        recruiter_active_days=data["recruiter_active_days"],
        recruiter_online=bool(data["recruiter_online"]),
        is_proxy_job=bool(data["is_proxy_job"]),
        job_invalid=bool(data["job_invalid"]),
        address=data["address"],
        recruiter_certificated=bool(data["recruiter_certificated"]),
        recruiter_id=data["recruiter_id"],
        brand_id=data["brand_id"],
        company_intro=data["company_intro"],
        company_founded_year=data["company_founded_year"],
        job_status_desc=data["job_status_desc"],
        position_name=data["position_name"],
        recruitment_count=data["recruitment_count"],
        latitude=data["latitude"],
        longitude=data["longitude"],
        job_description=data["job_description"],
        skills=json.loads(data["skills_json"] or "[]"),
        labels=json.loads(data["labels_json"] or "[]"),
        welfare=json.loads(data["welfare_json"] or "[]"),
        matched_queries=json.loads(data["matched_queries_json"] or "[]"),
        source_rank=data["source_rank"],
        raw=json.loads(data["raw_json"] or "{}"),
        retention_status=data["retention_status"],
        bookmarked=bool(data["bookmarked"]),
        collected_at=data["collected_at"],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--include-sample", action="store_true",
                        help="也给 sample 岗位补详情（默认只补 active，省详情配额）")
    args = parser.parse_args()

    # 带账号 cookie 的请求绝不能从陌生 IP 发出去（见 exit_guard 模块头）。
    # 放在读库之前：不一致时应该一条请求都不发。
    assert_home_exit()

    cfg = load_config(args.config)
    with JobStore(cfg.database_path) as store:
        status_filter = "" if args.include_sample else "AND retention_status = 'active' "
        # 多取一些候选，因为下面要按投递池判定过滤掉注定被降级的，
        # 命中率未知（实测存量里约 4 成注定降级），少取会凑不满 --limit。
        candidates = store.conn.execute(
            "SELECT * FROM jobs WHERE job_description = '' "
            f"AND raw_json != '{{}}' AND job_invalid = 0 {status_filter}"
            "ORDER BY canonical_id",
        ).fetchall()

        # ⚠️ 先按投递池判定筛一遍，再花配额。
        # 这些记录是**存量**，进库时的采集侧 `_should_keep` 拦不到它们
        # （那个只在列表阶段跑，而这里直接读库）。实测 5054 条待补详情里
        # 2167 条注定被 clean_local_db 降级（匿名公司 856、薪资超纲 2368，
        # 两者重叠 743）—— 一条详情一次请求，全跑完等于白花 120% 的日线额度。
        rows = []
        skipped: dict[str, int] = {}
        for r in candidates:
            demote, why = should_demote(r)
            if demote:
                key = why.split("(")[0].split("[")[0]
                skipped[key] = skipped.get(key, 0) + 1
                continue
            rows.append(r)
            if len(rows) >= args.limit:
                break

        if skipped:
            total_skipped = sum(skipped.values())
            print(f"跳过注定降级 {total_skipped} 条（省下同样次数的请求）："
                  f"{dict(sorted(skipped.items(), key=lambda kv: -kv[1]))}")
        print(f"待补详情：{len(rows)} 条")
        if not rows:
            return

        collector = BossCollector(cfg)
        # 注入请求账本：单日累计上限跨进程生效（封禁按累计量算）
        collector.ledger = store
        collector.ledger_script = "backfill_details"
        print(f"今日账本：{store.request_counts()}")
        await collector.start()
        # 页面停在 about:blank 时 _settle_on_origin 会卡住，先导航到搜索页建立
        # zhipin 域环境（详情接口的同源 fetch 依赖页面已在 zhipin 域）。
        first_city = cfg.cities[0]
        first_filter = cfg.filters[0] if cfg.filters else SearchFilters()
        nav_params = collector._params(first_city, cfg.keywords[0], first_filter, 1)
        nav_visible = {k: v for k, v in nav_params.items() if k not in ("pageSize", "scene")}
        from urllib.parse import urlencode as _urlencode
        await collector._goto_search(f"{SEARCH_BASE}?{_urlencode(nav_visible)}")
        print(f"已导航到搜索页: {collector.page.url[:80]}")
        done = 0
        failed = 0
        # 拟人化速率方案（2026-08-03）：
        # - 条内延迟高斯（boss.py），批次大小随机 4-6（打破固定模式）
        # - 批间大多短休 8-15s，每 2-4 批插入一次长停顿 25-50s + 拟人心跳（滚动页面保鲜）
        # - code37 触发时 _collect_detail 内建重算（完整重置窗口）
        from job_radar.throttle import HumanThrottle
        throttle = HumanThrottle(cfg, detail_batch=5, detail_cooldown=(10.0, 15.0))
        batch_size = random.randint(4, 6)
        within = 0
        batch_count = 0
        long_pause_after = random.randint(2, 4)
        # ⚠️ 原为硬编码 35，是"code=37 能预警封禁"这个**已被推翻的旧认知**的最后一处遗留
        # （2026-08-11 清理时漏了这个局部变量，它遮蔽了 collector 已读好的配置值）。
        # code=37 是无记忆配额窗口，实测每约 6 次请求必触发一次 → 35 次熔断等于
        # **把单次调用硬卡在约 205 次请求**，与日线无关。实测 08-11 03:33 就这样在
        # 170 条详情处掐断，而当日日线 1800 还剩一大截。
        # 真正的安全线是 max_requests_per_day（由 collector 的账本跨进程执行）。
        code37_limit = cfg.code37_limit
        try:
            for idx, row in enumerate(rows, start=1):
                # 熔断：重算过多说明信任度低，主动停一轮等静置，避免升到 code=32
                if collector.code37_count >= code37_limit:
                    raise RiskControlStop(
                        f"code=37 重算 {collector.code37_count} 次超阈值 {code37_limit}，主动停止防封禁")
                # 批次边界：大多短休，每 2-4 批一次长停顿 + 拟人心跳
                if within >= batch_size:
                    batch_count += 1
                    if batch_count >= long_pause_after:
                        dur = throttle._gaussian(25.0, 50.0)
                        print(f"\n[批次] 长停顿 {dur:.0f}s（拟人心跳）", flush=True)
                        await throttle.human_heartbeat(collector.page, dur)
                        long_pause_after = random.randint(2, 4)
                        batch_count = 0
                    else:
                        await asyncio.sleep(throttle._gaussian(10.0, 15.0))
                    batch_size = random.randint(4, 6)
                    within = 0
                    print(f"\n[批次] 下批 {batch_size} 条（{idx}/{len(rows)}）", flush=True)
                within += 1

                job = _row_to_job(row)
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    failed += 1
                    # 岗位已下架：标记无效并写库，避免下次续跑反复重试浪费请求
                    if "职位已不存在" in str(exc) or "已暂停" in str(exc):
                        job.job_invalid = True
                        store.upsert(job)
                        print(f"[下架] {job.title} | {job.company_name}")
                    else:
                        print(f"[跳过] {job.title} | {job.company_name}: {exc}")
                    continue
                store.upsert(job)
                done += 1
                if done % 4 == 0:
                    print(f"进度 {done}/{len(rows)}")
        except RiskControlStop as exc:
            print(f"命中风控，停止：{exc}")
        finally:
            print(f"完成 {done} 条，失败 {failed} 条，共发起请求 {collector.request_count} 次，code=37 重算 {collector.code37_count} 次")
            await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
