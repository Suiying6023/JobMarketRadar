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
from job_radar import profile  # noqa: E402
from job_radar.models import JobRecord, SearchFilters  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

# 投递池判定的唯一实现。这里复用它而不是另写一套，否则两份阈值会各自漂移。
from clean_local_db import should_demote  # noqa: E402


def _ai_skill_hits(row) -> int:
    """列表接口 `skills` 里命中了几个 AI 技术词。用于抓取优先级，不用于筛选。

    `skills` 是列表阶段就给的结构化技能词（短词、比 LLM 从 JD 抽的更干净），
    实测它是**唯一在列表阶段对最终判定有预测力的字段**：命中数越多 keep 率越高
    （0→65.2%、1→71.4%、2→74.3%、3+→80.3%）。其余列表字段
    （jobExperience/jobDegree/brandScaleName/jobLabels/welfareList）都无区分度。
    """
    try:
        skills = {str(s).lower() for s in (json.loads(row["raw_json"] or "{}").get("skills") or [])}
    except (json.JSONDecodeError, TypeError, AttributeError, IndexError, KeyError):
        return 0
    return sum(1 for k in profile.AI_TECH if any(k.lower() in s for s in skills))


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
        eligible = []
        skipped: dict[str, int] = {}
        for r in candidates:
            demote, why = should_demote(r)
            if demote:
                key = why.split("(")[0].split("[")[0]
                skipped[key] = skipped.get(key, 0) + 1
                continue
            eligible.append(r)

        if skipped:
            total_skipped = sum(skipped.values())
            print(f"跳过注定降级 {total_skipped} 条（省下同样次数的请求）："
                  f"{dict(sorted(skipped.items(), key=lambda kv: -kv[1]))}")

        # 按列表 `skills` 里的 AI 技术词命中数降序抓。
        # **这不是筛选，没有岗位被丢弃**，只是配额有限时让高命中率的先抓：
        # 实测 689 条已判定样本里，按命中数分组的 keep 率是
        # 0 个 65.2% / 1 个 71.4% / 2 个 74.3% / 3+ 个 80.3%，
        # 取命中数最高的前 30% 其 keep 率 84.0%（不排序是 69.7%）。
        # 之所以只能"排序"而不能"拦掉"：命中 0 个的那组 keep 率仍有 65.2%，
        # 拦掉会丢掉大量好岗位——列表字段的预测力够排序，不够当门槛。
        eligible.sort(key=lambda r: -_ai_skill_hits(r))
        rows = eligible[: args.limit]
        print(f"待补详情：{len(rows)} 条（共 {len(eligible)} 条合格，"
              f"按列表 skills 的 AI 技术词命中数优先）")
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
        # 抓之前重判时跳掉的（与选行阶段的 skipped 分开记：这些是跑的过程中
        # 状态发生变化的，量大说明库在被并发改动，值得知道）
        stale_skipped: dict[str, int] = {}
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

                # ⚠️ 花请求之前**重新核对一次**。候选列表是启动时一次性取完的，
                # 而 1000 条要跑 3.5 小时——这期间库里的状态完全可能已经变：
                # 另一个进程跑了 clean_local_db、或列表轮次更新了薪资/城市。
                # 实测 2026-08-29 本轮 273 条里 45 条注定被降级（其中 27 条的
                # 薪资/城市在列表阶段就已知），而选行那一刻它们还是 active。
                # 重读一行的开销是微秒级，一次详情请求是 3-5 秒且不可退——
                # 这个比价决定了该在每条前面重判，而不是只在选行时判一次。
                fresh = store.conn.execute(
                    "SELECT * FROM jobs WHERE canonical_id = ?",
                    (row["canonical_id"],),
                ).fetchone()
                if fresh is not None:
                    if fresh["job_description"]:
                        # 已被别的进程补过，别重复花配额
                        stale_skipped["已有JD"] = stale_skipped.get("已有JD", 0) + 1
                        continue
                    demote, why = should_demote(fresh)
                    if demote:
                        key = why.split("(")[0].split("[")[0]
                        stale_skipped[key] = stale_skipped.get(key, 0) + 1
                        continue
                    row = fresh

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
            # 用**唯一且好 grep 的标记**打头。长跑时这行会埋在几百行进度里，
            # 而它是唯一需要人立刻知道的事（`code=36/32` 要人工过验证）。
            # 监控只要 grep 这个标记，不必去解析进度行。
            print("!" * 68, flush=True)
            print(f"[RISK-STOP] 命中风控，已停止：{exc}", flush=True)
            print("[RISK-STOP] 若是 code=36/32，需要人工过验证："
                  "uv run python scripts/check_account_status.py", flush=True)
            print("!" * 68, flush=True)
        finally:
            print(f"完成 {done} 条，失败 {failed} 条，共发起请求 {collector.request_count} 次，code=37 重算 {collector.code37_count} 次")
            if stale_skipped:
                print(f"抓前重判跳过 {sum(stale_skipped.values())} 条"
                      f"（选行后状态变了，省下同样次数的请求）：{stale_skipped}")
            await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
