"""风控阈值探针：测量 BOSS 详情接口的真实 cookie 配额窗口。

2026-08-03 设计，回答三个问题：
1. **窗口大小**：从全新窗口开始，连续抓多少条才触发 code=37？
2. **恢复重置**：security-check 恢复后，还能连续抓几条再触发？（额度是重置还是累积？）
3. **冷却作用**：批间冷却 N 秒后，窗口恢复到什么程度？

测量方式：复用 backfill 的 _collect_detail 流程，逐条抓 pending 记录，
每次 code=37 触发时打印「距上次触发的间隔条数」。这个间隔就是当前窗口大小。

用法: python scripts/probe_throttle.py [--limit N] [--cooldown S]
  --limit     探测多少条（默认 40）
  --cooldown  批间冷却秒数；0=不冷却（连续抓，测原始窗口）
"""
from __future__ import annotations

import argparse
import asyncio
import json
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
        title=data["title"], company_name=data["company_name"],
        city=data["city"], district=data["district"],
        business_district=data["business_district"], location_text=data["location_text"],
        salary_text=data["salary_text"], salary_min_k=data["salary_min_k"],
        salary_max_k=data["salary_max_k"], salary_months=data["salary_months"],
        experience=data["experience"], education=data["education"],
        company_size=data["company_size"], financing_stage=data["financing_stage"],
        industry=data["industry"], recruiter_name=data["recruiter_name"],
        recruiter_title=data["recruiter_title"], recruiter_active_status=data["recruiter_active_status"],
        recruiter_active_days=data["recruiter_active_days"], recruiter_online=bool(data["recruiter_online"]),
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
        source_rank=data["source_rank"], raw=json.loads(data["raw_json"] or "{}"),
        collected_at=data["collected_at"],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--cooldown", type=float, default=0.0,
                        help="批间冷却秒数；0=连续抓测原始窗口")
    args = parser.parse_args()

    cfg = load_config(args.config)
    with JobStore(cfg.database_path) as store:
        rows = store.conn.execute(
            "SELECT * FROM jobs WHERE job_description = '' "
            "AND raw_json != '{}' AND job_invalid = 0 "
            "ORDER BY canonical_id LIMIT ?",
            (args.limit,),
        ).fetchall()
        print(f"待抓取：{len(rows)} 条 | cooldown={args.cooldown}s", flush=True)
        if not rows:
            return

        collector = BossCollector(cfg)
        await collector.start()
        first_city = cfg.cities[0]
        first_filter = cfg.filters[0] if cfg.filters else SearchFilters()
        nav_params = collector._params(first_city, cfg.keywords[0], first_filter, 1)
        nav_visible = {k: v for k, v in nav_params.items() if k not in ("pageSize", "scene")}
        from urllib.parse import urlencode as _urlencode
        await collector._goto_search(f"{SEARCH_BASE}?{_urlencode(nav_visible)}")
        print(f"已导航: {collector.page.url[:60]}", flush=True)

        prev_code37 = 0          # 上次打印时的 code37 总数
        since_last = 0           # 距上次 code37 的条数
        intervals = []           # 所有 code37 间隔
        done = 0
        try:
            for idx, row in enumerate(rows, start=1):
                since_last += 1
                job = _row_to_job(row)
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if "职位已不存在" in str(exc) or "已暂停" in str(exc):
                        job.job_invalid = True
                        store.upsert(job)
                        print(f"[{idx}] 下架: {job.title}", flush=True)
                        continue
                    print(f"[{idx}] 跳过: {job.title} | {str(exc)[:50]}", flush=True)
                    continue
                store.upsert(job)
                done += 1
                if collector.code37_count > prev_code37:
                    intervals.append(since_last)
                    print(f"[{idx}] ★ code=37 触发! 距上次 {since_last} 条 "
                          f"(done={done}) 累计间隔: {intervals}", flush=True)
                    prev_code37 = collector.code37_count
                    since_last = 0
                    if args.cooldown > 0:
                        print(f"    批间冷却 {args.cooldown}s...", flush=True)
                        await asyncio.sleep(args.cooldown)
                elif done % 5 == 0:
                    print(f"[{idx}] done={done} (code37={collector.code37_count})", flush=True)
        except RiskControlStop as exc:
            print(f"!! 风控停止: {exc}", flush=True)
        finally:
            print(f"\n=== 探测结果 ===", flush=True)
            print(f"抓取 {done} 条 | code=37 触发 {collector.code37_count} 次 | "
                  f"间隔序列 {intervals}", flush=True)
            if intervals:
                print(f"平均窗口: {sum(intervals)/len(intervals):.1f} 条/触发 "
                      f"(min={min(intervals)}, max={max(intervals)})", flush=True)
            print(f"总请求 {collector.request_count} 次", flush=True)
            await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
