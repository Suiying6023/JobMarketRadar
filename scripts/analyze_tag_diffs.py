"""纯本地差异分析：规则标签(jd_tags_json) vs LLM 标签(glm_tags_json)。

找出用户关心的不一致项：
- 学历冲突：规则标本科，JD 实际要求硕士/博士
- 名校要求：LLM 识别到 985/211/一本/双一流，规则漏提
- 经验冲突：规则标接受应届，JD 实际要求 ≥N 年
- 其他硬性门槛：英语/出差/驻场/证书/专业 等规则漏提

用法: python scripts/analyze_tag_diffs.py
"""
from __future__ import annotations

import json
import re
import sqlite3

DB = "data/hangzhou_agent.sqlite3"


def compare_one(rule: dict, glm: dict) -> list[str]:
    diffs: list[str] = []
    rule_edu = set(rule.get("edu", []))
    rule_exp = set(rule.get("exp", []))
    rule_other = set(rule.get("other", []))
    g_edu = glm.get("edu_req") or []
    g_school = glm.get("school_req") or []
    g_exp = glm.get("exp_req") or []
    g_other = glm.get("other_req") or []

    # 1. 学历冲突：规则没标，JD 明确要求硕士/博士
    for e in g_edu:
        if "硕士" in e and "硕士+" not in rule_edu and "本科+" not in rule_edu or \
           "硕士" in e and "硕士+" not in rule_edu:
            diffs.append(f"学历要求硕士: JD「{e}」，规则未标硕士")
        if "博士" in e and "博士" not in rule_edu:
            diffs.append(f"学历要求博士: JD「{e}」，规则未标博士")
    # 2. 名校漏提
    for s in g_school:
        if any(k in s for k in ("985", "211", "双一流", "一本", "重点", "知名")) and "名校985/211" not in rule_edu:
            diffs.append(f"名校要求: JD「{s}」，规则漏提")
    # 3. 经验冲突
    for e in g_exp:
        m = re.search(r"(\d+)\s*年", e)
        if m:
            years = int(m.group(1))
            if years >= 2 and "接受应届/无经验" in rule_exp:
                diffs.append(f"经验冲突: 规则标接受应届，JD 实际要求「{e}」")
            elif years >= 3 and not any(f"≥{x}" in r for r in rule_exp for x in ("3年", "5年")):
                diffs.append(f"经验漏提: JD 明确「{e}」，规则未标")
    # 4. 其他硬性漏提
    for o in g_other:
        for kw, label in (("英语", "英语要求"), ("CET", "英语要求"), ("六级", "英语要求"),
                          ("出差", "需出差"), ("驻场", "驻场/外包"), ("外包", "驻场/外包"),
                          ("证书", "证书要求"), ("PMP", "证书要求"), ("软考", "证书要求"),
                          ("专业", "专业要求")):
            if kw in o and label not in rule_other:
                diffs.append(f"其他漏提: JD「{o[:40]}」")
                break
    return diffs


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, company_name, salary_text, jd_tags_json, glm_tags_json "
        "FROM jobs WHERE glm_tags_updated_at != ''"
    ).fetchall()

    all_diffs: list[tuple] = []
    counters: dict[str, int] = {"学历硕士": 0, "学历博士": 0, "名校": 0, "经验冲突": 0, "经验漏提": 0, "其他": 0}
    for r in rows:
        rule = json.loads(r["jd_tags_json"] or "{}")
        glm = json.loads(r["glm_tags_json"] or "{}")
        diffs = compare_one(rule, glm)
        for d in diffs:
            for k in counters:
                if k in d:
                    counters[k] += 1
        if diffs:
            all_diffs.append((r, diffs))

    print(f"对比 {len(rows)} 条，其中 {len(all_diffs)} 条存在差异\n")
    print("=== 差异类型统计 ===")
    for k, v in counters.items():
        print(f"  {k}: {v}")
    print(f"\n差异岗位明细（前 40 条）:")
    for r, diffs in all_diffs[:40]:
        print(f"\n  ■ {r['title'][:24]} | {r['company_name'][:14]} | {r['salary_text']}")
        for d in diffs:
            print(f"      ⚠ {d}")

    # 导出
    import csv
    with open("output/标签差异清单.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["岗位", "公司", "薪资", "差异说明"])
        for r, diffs in all_diffs:
            for d in diffs:
                w.writerow([r["title"], r["company_name"], r["salary_text"], d])
    print(f"\n已导出 output/标签差异清单.csv: {sum(len(d) for _, d in all_diffs)} 条差异记录")


if __name__ == "__main__":
    main()
