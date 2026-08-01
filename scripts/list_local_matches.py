"""本地数据过滤：列出符合目标画像的岗位清单。

目标画像（用户确认）：
- 全职（排除实习/兼职，靠日薪/标题识别）
- 学历本科及以下（本科/大专/学历不限/中专及以下）
- 经验：在校/应届、经验不限、1年以内、1-3年
- 排除：已失效、明显代招/外包

输出符合条件的岗位明细 + 统计。
"""
from __future__ import annotations

import json
import sqlite3

con = sqlite3.connect('data/hangzhou_agent.sqlite3')
con.row_factory = sqlite3.Row

# 本地经验值（合并了在校/应届为一个值）
ALLOW_EXP = {"在校/应届", "经验不限", "1年以内", "1-3年"}
# 本地学历值（学历不限算本科及以下档）
ALLOW_DEG = {"本科", "大专", "学历不限", "中专/中技", "高中", "初中及以下"}
# 排除词（标题）
EXCLUDE_TITLE = ["销售", "主播", "客服", "电话", "猎头", "劳务派遣"]
# 日薪特征 = 实习
DAILY_MARKERS = ["元/天", "元/日"]
# 明显外包/人力公司
EXCLUDE_COMPANY = ["猎头", "劳务派遣", "人力资源"]


def is_daily(salary: str) -> bool:
    return any(m in (salary or "") for m in DAILY_MARKERS)


rows = con.execute(
    "SELECT * FROM jobs ORDER BY "
    "CASE WHEN job_description != '' THEN 0 ELSE 1 END, first_seen_at DESC"
).fetchall()

matched = []
for r in rows:
    exp = r["experience"] or ""
    deg = r["education"] or ""
    title = (r["title"] or "").lower()
    company = (r["company_name"] or "").lower()

    if r["job_invalid"]:
        continue
    if exp not in ALLOW_EXP:
        continue
    if deg not in ALLOW_DEG:
        continue
    if is_daily(r["salary_text"] or ""):
        continue
    if any(kw in title for kw in EXCLUDE_TITLE):
        continue
    if any(kw in company for kw in EXCLUDE_COMPANY):
        continue

    matched.append({
        "title": r["title"] or "",
        "company": r["company_name"] or "",
        "salary": r["salary_text"] or "",
        "exp": exp,
        "deg": deg,
        "has_jd": bool(r["job_description"]),
        "active": (r["recruiter_active_status"] or "")[:20],
        "proxy": bool(r["is_proxy_job"]),
        "size": r["company_size"] or "",
        "industry": r["industry"] or "",
    })

print(f"符合目标画像: {len(matched)} 条\n")
print("=== 明细 ===")
for i, m in enumerate(matched, 1):
    jd_mark = "📄" if m["has_jd"] else "  "
    proxy_mark = "🔄" if m["proxy"] else "  "
    print(f"{i:3}. {jd_mark}{proxy_mark} {m['title'][:24]:26} | {m['salary']:12} | "
          f"{m['exp']:6} {m['deg']:4} | {m['company'][:16]:18} | {m['active']}")

print(f"\n统计:")
print(f"  符合: {len(matched)}")
print(f"  有 JD: {sum(1 for m in matched if m['has_jd'])}")
print(f"  无 JD（可补）: {sum(1 for m in matched if not m['has_jd'])}")
print(f"  代招: {sum(1 for m in matched if m['proxy'])}")
