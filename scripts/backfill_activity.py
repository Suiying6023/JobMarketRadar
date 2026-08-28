"""给活跃度为空的岗位重抓详情，补 recruiter_active_status（P1 待确认）。

P1 = active + 已补 JD 但活跃度空（历史批次 detail 没写 raw 或当时被节流）。
重抓 detail 会刷新 activeTimeDesc → recruiter_active_status，并更新 raw 的 detail。

保守节奏（解封后信任度低）：batch=5 + 批间主动铸造/短休，limit 默认 150（封禁安全上限）。

用法: python scripts/backfill_activity.py --limit 150
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import SEARCH_BASE, BossCollector, RiskControlStop  # noqa: E402
from job_radar.config import load_config  # noqa: E402
from job_radar.models import JobRecord, SearchFilters  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402


def _row_to_job(row) -> JobRecord:
    data = dict(row)
    return JobRecord(
        platform=data["platform"], source_job_id=data["source_job_id"],
        job_url=data["job_url"], company_url=data["company_url"],
        title=data["title"], company_name=data["company_name"], city=data["city"],
        district=data["district"], business_district=data["business_district"],
        location_text=data["location_text"], salary_text=data["salary_text"],
        salary_min_k=data["salary_min_k"], salary_max_k=data["salary_max_k"],
        salary_months=data["salary_months"], experience=data["experience"],
        education=data["education"], company_size=data["company_size"],
        financing_stage=data["financing_stage"], industry=data["industry"],
        recruiter_name=data["recruiter_name"], recruiter_title=data["recruiter_title"],
        recruiter_active_status=data["recruiter_active_status"],
        recruiter_active_days=data["recruiter_active_days"],
        recruiter_online=bool(data["recruiter_online"]),
        is_proxy_job=bool(data["is_proxy_job"]), job_invalid=bool(data["job_invalid"]),
        address=data["address"], recruiter_certificated=bool(data["recruiter_certificated"]),
        recruiter_id=data["recruiter_id"], brand_id=data["brand_id"],
        company_intro=data["company_intro"], company_founded_year=data["company_founded_year"],
        job_status_desc=data["job_status_desc"], position_name=data["position_name"],
        recruitment_count=data["recruitment_count"], latitude=data["latitude"],
        longitude=data["longitude"], job_description=data["job_description"],
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
    args = parser.parse_args()

    cfg = load_config(args.config)
    with JobStore(cfg.database_path) as store:
        rows = store.conn.execute(
            "SELECT * FROM jobs WHERE retention_status = 'active' "
            "AND job_description != '' AND recruiter_active_status = '' "
            "AND raw_json != '{}' AND job_invalid = 0 "
            "ORDER BY canonical_id LIMIT ?",
            (args.limit,),
        ).fetchall()
        print(f"活跃度待补: {len(rows)} 条")
        if not rows:
            return

        collector = BossCollector(cfg)
        # 注入请求账本：单日累计上限跨进程生效（封禁按累计量算）
        collector.ledger = store
        collector.ledger_script = "backfill_activity"
        print(f"今日账本：{store.request_counts()}")
        await collector.start()
        first_city = cfg.cities[0]
        first_filter = cfg.filters[0] if cfg.filters else SearchFilters()
        nav_params = collector._params(first_city, cfg.keywords[0], first_filter, 1)
        nav_visible = {k: v for k, v in nav_params.items() if k not in ("pageSize", "scene")}
        from urllib.parse import urlencode as _urlencode
        await collector._goto_search(f"{SEARCH_BASE}?{_urlencode(nav_visible)}")
        print(f"已导航: {collector.page.url[:80]}")

        from job_radar.throttle import HumanThrottle
        throttle = HumanThrottle(cfg, detail_batch=5, detail_cooldown=(10.0, 15.0))
        batch_size = random.randint(4, 6)
        within = 0
        batch_count = 0
        long_pause_after = random.randint(2, 4)
        done = 0
        failed = 0
        try:
            for idx, row in enumerate(rows, start=1):
                # 熔断：重算过多说明信任度低，主动停一轮等静置，避免升到 code=32
                # 阈值读配置（原硬编码 35，是"code=37 预警封禁"旧认知的遗留）。
                # 详见 backfill_details.py 同处注释：35 次 ≈ 只够 205 次请求。
                if collector.code37_count >= cfg.code37_limit:
                    raise RiskControlStop(
                        f"code=37 重算 {collector.code37_count} 次超阈值 "
                        f"{cfg.code37_limit}，主动停止防封禁")
                # 拟人化批间：大多短休，每 2-4 批一次长停顿 + 拟人心跳
                if within >= batch_size:
                    batch_count += 1
                    if batch_count >= long_pause_after:
                        dur = throttle._gaussian(25.0, 50.0)
                        print(f"[批次] 长停顿 {dur:.0f}s（拟人心跳）", flush=True)
                        await throttle.human_heartbeat(collector.page, dur)
                        long_pause_after = random.randint(2, 4)
                        batch_count = 0
                    else:
                        await asyncio.sleep(throttle._gaussian(10.0, 15.0))
                    batch_size = random.randint(4, 6)
                    within = 0
                    print(f"[批次] 下批 {batch_size} 条（{idx}/{len(rows)}）", flush=True)
                within += 1

                job = _row_to_job(row)
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    failed += 1
                    if "职位已不存在" in str(exc) or "已暂停" in str(exc):
                        job.job_invalid = True
                        store.upsert(job)
                    else:
                        print(f"[跳过] {job.title[:20]}: {str(exc)[:50]}")
                    continue
                store.upsert(job)
                done += 1
                if done % 10 == 0:
                    print(f"进度 {done}/{len(rows)}", flush=True)
        except RiskControlStop as exc:
            print(f"命中风控，停止：{exc}")
        finally:
            print(f"完成 {done} 条，失败 {failed} 条，code=37 重算 {collector.code37_count} 次")
            await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
