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
            "SELECT * FROM jobs WHERE job_description = '' "
            "AND raw_json != '{}' ORDER BY canonical_id LIMIT ?",
            (args.limit,),
        ).fetchall()
        print(f"待补详情：{len(rows)} 条")
        if not rows:
            return

        collector = BossCollector(cfg)
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
        # code=37（stoken 过期）已在 _same_origin_fetch 内部自动走 security-check
        # 重算重试（约 10 秒/次），不再需要批间长冷却。仅真正的硬风控（32/36）会抛
        # RiskControlStop 停止整轮。
        try:
            for row in rows:
                job = _row_to_job(row)
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    failed += 1
                    print(f"[跳过] {job.title} | {job.company_name}: {exc}")
                    continue
                store.upsert(job)
                done += 1
                if done % 10 == 0:
                    print(f"进度 {done}/{len(rows)}")
        except RiskControlStop as exc:
            print(f"命中风控，停止：{exc}")
        finally:
            print(f"完成 {done} 条，失败 {failed} 条，共发起请求 {collector.request_count} 次")
            await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
