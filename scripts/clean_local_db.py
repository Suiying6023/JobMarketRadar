"""本地数据清理：删除不符合目标画像的记录。

口径（用户确认）：
- 经验：在校/应届、经验不限、1年以内、1-3年
- 学历：本科及以下（本科/大专/学历不限/中专及以下）
- 薪资：排除 salary_min_k > 25（起点就超25K的高薪岗）
- 排除：代招(is_proxy_job)、已失效(job_invalid)、日薪(实习)、标题含销售/客服等
- 排除公司：猎头/劳务派遣/人力资源

用法:
  python scripts/clean_local_db.py --dry-run   # 只统计，不删
  python scripts/clean_local_db.py             # 实际删除
"""
from __future__ import annotations

import sqlite3
import sys

DB = "data/hangzhou_agent.sqlite3"

ALLOW_EXP = {"在校/应届", "经验不限", "1年以内", "1-3年"}
ALLOW_DEG = {"本科", "大专", "学历不限", "中专/中技", "高中", "初中及以下"}
EXCLUDE_TITLE = ["销售", "主播", "客服", "电话", "猎头", "劳务派遣"]
EXCLUDE_COMPANY = ["猎头", "劳务派遣", "人力资源"]
DAILY_MARKERS = ["元/天", "元/日"]
HIGH_SALARY_MIN = 20.0


def is_daily(salary: str) -> bool:
    return any(m in (salary or "") for m in DAILY_MARKERS)


def should_delete(r) -> tuple[bool, str]:
    exp = r["experience"] or ""
    deg = r["education"] or ""
    title = (r["title"] or "").lower()
    company = (r["company_name"] or "").lower()
    salary = r["salary_text"] or ""

    if r["job_invalid"]:
        return True, "已失效"
    if r["is_proxy_job"]:
        return True, "代招"
    if r["salary_min_k"] is not None and r["salary_min_k"] > HIGH_SALARY_MIN:
        return True, f"高薪(下限{r['salary_min_k']:.0f}K)"
    if is_daily(salary):
        return True, "日薪(实习)"
    if exp not in ALLOW_EXP:
        return True, f"经验[{exp}]"
    if deg not in ALLOW_DEG:
        return True, f"学历[{deg}]"
    if any(kw in title for kw in EXCLUDE_TITLE):
        return True, f"标题[{kw}]"
    if any(kw in company for kw in EXCLUDE_COMPANY):
        return True, f"公司[{kw}]"
    return False, ""


def main() -> None:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = con.execute("SELECT * FROM jobs").fetchall()
    total = len(rows)
    to_delete = []
    reasons: dict[str, int] = {}
    for r in rows:
        del_flag, reason = should_delete(r)
        if del_flag:
            to_delete.append((r["canonical_id"], r["title"] or "", reason))
            reasons[reason] = reasons.get(reason, 0) + 1

    print(f"总量: {total}")
    print(f"将删除: {len(to_delete)} 条，保留: {total - len(to_delete)} 条")
    print(f"\n删除原因分布:")
    for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {n}")

    if dry:
        print("\n[DRY-RUN] 未执行删除。加 --dry-run 参数移除即实际删除")
        print("\n删除样例（前 20 条）:")
        for cid, title, reason in to_delete[:20]:
            print(f"  [{reason}] {title[:36]}")
    else:
        con.execute("BEGIN")
        for cid, _t, _r in to_delete:
            con.execute("DELETE FROM jobs WHERE canonical_id = ?", (cid,))
        con.commit()
        print(f"\n已删除 {len(to_delete)} 条")

    con.close()


if __name__ == "__main__":
    main()
