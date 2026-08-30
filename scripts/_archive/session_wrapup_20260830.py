"""一次性：收工盘点（2026-08-30 刷新链路优化那轮）。留档，不再维护。"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent.parent / "data" / "hangzhou_agent.sqlite3"

POOL = ("SELECT count(*) FROM jobs WHERE retention_status='active' "
        "AND job_invalid=0 AND job_status_desc!='职位已关闭' "
        "AND job_description!=''")
CLOSED = "SELECT count(*) FROM jobs WHERE job_status_desc='职位已关闭'"
TZ_LEFT = "SELECT count(*) FROM jobs WHERE updated_at LIKE '%T%'"
STALE = ("SELECT count(*) FROM jobs WHERE retention_status='active' "
         "AND job_invalid=0 AND job_status_desc!='职位已关闭' "
         "AND job_description!='' AND (detail_fetched_at IS NULL "
         "OR detail_fetched_at='' OR substr(detail_fetched_at,1,10) "
         "< date('now','-7 days'))")


def main() -> None:
    con = sqlite3.connect(DB)
    day = datetime.now().strftime("%Y-%m-%d")

    refresh = [o for (o,) in con.execute(
        "SELECT outcome FROM request_log WHERE day=? AND kind='detail_page'",
        (day,)).fetchall()]
    collect = con.execute(
        "SELECT count(*) FROM request_log WHERE day=? "
        "AND kind IN ('detail','list','security_check')", (day,)).fetchone()[0]

    print("=== 今日账本 ===")
    print(f"  刷新链路 detail_page : {len(refresh)} / 1000（自设日线）")
    print(f"    {dict(Counter(refresh))}")
    print(f"  采集链路（账号侧）   : {collect} / 900  ← 全天未动，账号配额完好")

    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    print("\n=== 库状态 ===")
    print(f"  投递池待刷（有 JD 的 active）: {q(POOL)} 条")
    print(f"  其中快照 > 7 天（真需要刷）  : {q(STALE)} 条")
    print(f"  累计已关闭并降级             : {q(CLOSED)} 条（今日开工时 162）")
    print(f"  updated_at 时区分裂残留      : {q(TZ_LEFT)} 行（应为 0）")
    con.close()


if __name__ == "__main__":
    main()
