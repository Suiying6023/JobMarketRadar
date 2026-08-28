"""标记/取消标记浏览或投递过的岗位（bookmarked=1）。

投递池 = active OR bookmarked。画像外的岗位一旦被标记浏览就保留为投递目标，
不会被 clean_local_db 降级成 sample。

按 canonical_id / source_job_id / job_url 任意一种标识匹配：
  python scripts/mark_bookmarked.py zp:<job_id>          # canonical_id
  python scripts/mark_bookmarked.py <source_job_id>      # 源岗位 ID
  python scripts/mark_bookmarked.py "https://www.zhipin.com/job_detail/xxx.html"
  python scripts/mark_bookmarked.py --unmark <id>        # 取消标记
  python scripts/mark_bookmarked.py --unmark --all       # 清除全部标记
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.storage import JobStore  # noqa: E402


def resolve_rows(store: JobStore, value: str) -> list:
    """按 canonical_id / source_job_id / job_url 依次尝试匹配。"""
    con = store.conn
    # 精确 canonical_id（形如 "boss:<id>" 或裸 id）
    rows = con.execute(
        "SELECT canonical_id, title, company_name, job_url, retention_status, bookmarked "
        "FROM jobs WHERE canonical_id = ? OR canonical_id = 'boss:' || ? OR source_job_id = ? "
        "OR job_url = ? LIMIT 10",
        (value, value, value, value),
    ).fetchall()
    if rows:
        return list(rows)
    # job_url 模糊匹配（带 query 参数等差异）
    return list(con.execute(
        "SELECT canonical_id, title, company_name, job_url, retention_status, bookmarked "
        "FROM jobs WHERE job_url LIKE ? LIMIT 10",
        (f"%{value}%",),
    ).fetchall())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("identifiers", nargs="*", help="canonical_id / source_job_id / job_url")
    parser.add_argument("--unmark", action="store_true", help="取消标记（默认是标记）")
    parser.add_argument("--all", action="store_true", help="配合 --unmark 清除全部 bookmarked")
    parser.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    args = parser.parse_args()

    if args.unmark and args.all:
        with JobStore(args.db) as store:
            store.conn.execute("UPDATE jobs SET bookmarked = 0")
            store.conn.commit()
            print("已清除全部 bookmarked 标记")
        return

    if not args.identifiers:
        parser.error("缺少岗位标识（canonical_id / source_job_id / job_url）")

    with JobStore(args.db) as store:
        total = 0
        for value in args.identifiers:
            rows = resolve_rows(store, value)
            if not rows:
                print(f"!! 未匹配: {value[:50]}")
                continue
            for r in rows:
                action = "取消标记" if args.unmark else "标记"
                if args.unmark:
                    store.conn.execute("UPDATE jobs SET bookmarked = 0 WHERE canonical_id = ?", (r["canonical_id"],))
                else:
                    store.conn.execute("UPDATE jobs SET bookmarked = 1 WHERE canonical_id = ?", (r["canonical_id"],))
                store.conn.commit()
                total += 1
                status = "sample" if r["retention_status"] == "sample" else "active"
                print(f"[{action}] {r['title'][:24]} | {r['company_name'][:14]} | {r['canonical_id'][:28]} | {status}")
        print(f"\n共 {total} 条")

    # 单独统计分层变化（bookmarked 后 sample→active 的投递池）
    with JobStore(args.db) as store:
        pool = store.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE retention_status = 'active' OR bookmarked = 1"
        ).fetchone()[0]
        sample = store.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE retention_status = 'sample' AND bookmarked = 0"
        ).fetchone()[0]
        print(f"投递池(active OR bookmarked): {pool} | 样本(sample 且未标记): {sample}")


if __name__ == "__main__":
    main()
