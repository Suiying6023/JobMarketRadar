"""本地数据过滤分析：按用户目标画像统计库里岗位的分布。

目标画像（用户确认）：
- 全职（排除实习/兼职）
- 学历本科及以下
- 经验：应届/经验不限/1年以内/1-3年
- 排除明显非目标：销售/外包/猎头等

输出：
1. 按各维度的分布
2. 符合/不符合的统计
3. 不符合的原因分类（实习、高经验、销售、外包...）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar import profile as P  # noqa: E402

con = sqlite3.connect('data/hangzhou_agent.sqlite3')
con.row_factory = sqlite3.Row

# 分层视角：分析只看 active（画像内投递池）；sample 是去重样本不参与画像统计。
print("=== 分层统计 ===")
for r in con.execute("SELECT retention_status, COUNT(*) c FROM jobs GROUP BY retention_status"):
    print(f"  {r['retention_status']}: {r['c']}")
pool = con.execute("SELECT COUNT(*) FROM jobs WHERE retention_status = 'active' OR bookmarked = 1").fetchone()[0]
print(f"  投递池(active OR bookmarked): {pool}")
print()

rows = con.execute("SELECT * FROM jobs WHERE retention_status = 'active'").fetchall()

# 2026-08-10 修正：原先本地写 ALLOW_EXP={"应届生",...} 用的是 BOSS **搜索参数**
# 字面值，但库里 experience 存的是列表接口返回的「在校/应届」，导致这个脚本把
# 唯一真正的应届岗（11 条）判成不符合；ALLOW_DEG 也漏了「学历不限」误报 7 条。
# 现统一引用 profile 单一真源。
ALLOW_EXP = P.ALLOW_EXP
ALLOW_DEG = P.ALLOW_DEG
EXCLUDE_TITLE = list(P.EXCLUDE_TITLE)

total = len(rows)

print(f"总量: {total} 条\n")

# 1. 各维度分布
def dist(field):
    d = {}
    for r in rows:
        v = r[field] or "(空)"
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items(), key=lambda x: -x[1])[:8])

for field, label in [("experience", "经验"), ("education", "学历"), ("job_invalid", "已失效"),
                     ("is_proxy_job", "代招"), ("company_size", "公司规模")]:
    print(f"=== {label} 分布 ===")
    for k, v in dist(field).items():
        print(f"  {k}: {v}")
    print()

# 2. 委托/代招统计（外包特征）
proxy = sum(1 for r in rows if r["is_proxy_job"])
invalid = sum(1 for r in rows if r["job_invalid"])
print(f"代招(is_proxy_job): {proxy}  已失效(job_invalid): {invalid}\n")

# 3. 按标题排除词分类
print("=== 标题含排除词 ===")
for kw in EXCLUDE_TITLE:
    n = sum(1 for r in rows if kw.lower() in (r["title"] or "").lower())
    print(f"  「{kw}」: {n} 条")
    if n:
        for r in rows:
            if kw.lower() in (r["title"] or "").lower():
                print(f"      - {r['title'][:30]} | {r['company_name'][:16]} | {r['salary_text']}")
print()

# 4. 是否符合目标画像
match = 0
mismatch_exp = mismatch_deg = 0
for r in rows:
    exp_ok = (r["experience"] or "") in ALLOW_EXP
    deg_ok = (r["education"] or "") in ALLOW_DEG
    if exp_ok and deg_ok:
        match += 1
    if not exp_ok:
        mismatch_exp += 1
    if not deg_ok:
        mismatch_deg += 1
print(f"=== 目标画像匹配统计 ===")
print(f"  符合(经验+学历都对): {match} 条")
print(f"  经验不符合: {mismatch_exp} 条")
print(f"  学历不符合: {mismatch_deg} 条")
