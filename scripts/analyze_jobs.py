"""岗位统计分析：基于分层后的投递池数据生成画像报告。

输出：
1. 数据总览（分层/补全状态）
2. 投递池画像（active）：区域/薪资/规模/融资/行业/活跃度
3. LLM 标签聚合：job_type / 技术栈 / 经验要求 / 学历院校 / 其他门槛
4. 值得投递的高价值岗位（薪资×活跃度×岗位类型）

用法: python scripts/analyze_jobs.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.profile import ACTIVE_P0_MAX_DAYS  # noqa: E402

DB = "data/hangzhou_agent.sqlite3"


def dist(rows, field, top=8):
    c = Counter(r[field] or "(空)" for r in rows)
    return c.most_common(top)


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # 1. 数据总览
    print("=" * 56)
    print("杭州 AI/Agent 岗位库 — 统计分析")
    print("=" * 56)
    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    active = con.execute("SELECT COUNT(*) FROM jobs WHERE retention_status='active'").fetchone()[0]
    sample = con.execute("SELECT COUNT(*) FROM jobs WHERE retention_status='sample'").fetchone()[0]
    active_jd = con.execute("SELECT COUNT(*) FROM jobs WHERE retention_status='active' AND job_description != ''").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM jobs WHERE retention_status='active' AND job_description = ''").fetchone()[0]
    print(f"\n[数据总览] 共 {total} 条")
    print(f"  active(投递池): {active} | sample(样本): {sample}")
    print(f"  active 已补 JD: {active_jd} | 待补详情: {pending}")

    rows = con.execute(
        "SELECT * FROM jobs WHERE retention_status='active' AND job_description != ''"
    ).fetchall()
    # 按天数判而非文案白名单：白名单会漏掉中间档（本周/2周内/本月…），
    # 那些行既不进 P0 也不进 P1，在统计里凭空消失。
    p0 = [r for r in rows
          if (r["recruiter_active_days"] is not None
              and r["recruiter_active_days"] <= ACTIVE_P0_MAX_DAYS)]
    print(f"  投递池(active+JD): {len(rows)} | "
          f"P0优先(≤{ACTIVE_P0_MAX_DAYS}天活跃): {len(p0)}")

    # 2. 投递池画像
    print("\n[投递池画像 active={}]".format(len(rows)))
    for field, label, top in [("district", "区域", 8), ("company_size", "公司规模", 6),
                              ("financing_stage", "融资阶段", 6), ("industry", "行业", 8)]:
        print(f"  {label}:")
        for k, v in dist(rows, field, top):
            print(f"    {k}: {v}")

    # 薪资
    sal = [r for r in rows if r["salary_min_k"] and r["salary_max_k"]]
    mid = sorted((r["salary_min_k"] + r["salary_max_k"]) / 2 for r in sal)
    p50 = mid[len(mid) // 2] if mid else 0
    p75 = mid[int(len(mid) * 0.75)] if mid else 0
    hi = sum(1 for r in sal if r["salary_max_k"] >= 30)
    print(f"  薪资(有效 {len(sal)}): 中位 {p50:.0f}K | P75 {p75:.0f}K | max≥30K 有 {hi} 条")

    # 活跃度
    print(f"  活跃度: {dict(Counter(r['recruiter_active_status'] or '(空)' for r in rows).most_common(6))}")

    # 3. LLM 标签聚合
    print("\n[LLM 标签聚合]")
    jt = Counter((json.loads(r["glm_tags_json"] or "{}").get("job_type", "?")) for r in rows)
    print(f"  job_type: {dict(jt.most_common())}")

    tech = Counter()
    exp = Counter()
    edu = Counter()
    school = Counter()
    other = Counter()
    for r in rows:
        g = json.loads(r["glm_tags_json"] or "{}")
        for t in g.get("tech_req", []):
            tech[t] += 1
        for e in g.get("exp_req", []):
            exp[e] += 1
        for e in g.get("edu_req", []):
            edu[e] += 1
        for s in g.get("school_req", []):
            school[s] += 1
        for o in g.get("other_req", []):
            other[o] += 1
    print("  技术栈 top 15:")
    for t, n in tech.most_common(15):
        print(f"    {t}: {n}")
    print("  学历要求 top 5:", dict(edu.most_common(5)))
    print("  院校要求:", dict(school.most_common(5)) or "  (无)")
    print("  其他门槛 top 8:")
    for o, n in other.most_common(8):
        print(f"    {o}: {n}")

    # 4. 高价值岗位（P0 + 开发/算法 + 薪资高）
    print("\n[值得投递的高价值岗位 P0+开发/算法，按薪资]")
    candidates = [r for r in p0 if json.loads(r["glm_tags_json"] or "{}").get("job_type") in ("开发岗", "算法岗")]
    candidates.sort(key=lambda r: r["salary_max_k"] or 0, reverse=True)
    for r in candidates[:15]:
        g = json.loads(r["glm_tags_json"] or "{}")
        print(f"  {r['title'][:22]} | {r['company_name'][:13]} | {r['salary_text']} | "
              f"{r['district']} | {g.get('job_type')} | {r['recruiter_active_status']}")

    con.close()


if __name__ == "__main__":
    main()
