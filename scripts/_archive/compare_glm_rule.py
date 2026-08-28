"""对比规则提取标签（jd_tags_json）与 GLM 提取结果，找出差异。

用户关注：已有标签和 JD 内容不一致的——
- 学位/院校要求冲突（标签写本科，JD 实际要求硕士/名校）
- 经验要求冲突（标签接受应届，JD 实际要 N 年经验）
- 其他硬性要求漏提（英语/出差/证书/驻场/专业要求）

用法: python scripts/compare_glm_rule.py --limit 20 --concurrency 5
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_glm_labels import call_glm, load_key  # noqa: E402

# 规则标签里这些词与 GLM 的院校要求对应
EDU_HARD = {"本科+", "硕士+", "博士", "名校985/211", "全日制", "留学背景"}
EXP_RULE = {"接受应届/无经验", "经验不限"}
OTHER_HARD = {"英语要求", "需出差", "驻场/外包", "证书要求", "尽快到岗", "抗压/加班"}


def compare_one(rule: dict, glm: dict) -> list[str]:
    """返回差异说明列表。"""
    diffs = []
    rule_edu = set(rule.get("edu", []))
    rule_exp = set(rule.get("exp", []))
    rule_other = set(rule.get("other", []))
    g_edu = glm.get("edu_req") or []
    g_school = glm.get("school_req") or []
    g_exp = glm.get("exp_req") or []
    g_other = glm.get("other_req") or []
    g_bonus = glm.get("bonus") or []

    # 1. 院校/学历：GLM 说要求硕士/博士，规则只标了本科
    for e in g_edu:
        if "硕士" in e and "硕士+" not in rule_edu:
            diffs.append(f"学历冲突: 规则未标硕士，JD 实际要求「{e}」")
        if "博士" in e and "博士" not in rule_edu:
            diffs.append(f"学历冲突: 规则未标博士，JD 实际要求「{e}」")
    # 2. 名校漏提：GLM 识别到 985/211/一本/双一流
    for s in g_school:
        if any(k in s for k in ("985", "211", "双一流", "一本", "重点")) and "名校985/211" not in rule_edu:
            diffs.append(f"名校要求: 规则漏提「{s}」")
    # 3. 经验冲突：规则接受应届，但 JD 明确要 N 年
    for e in g_exp:
        import re
        m = re.search(r"(\d+)\s*年", e)
        if m and "接受应届/无经验" in rule_exp and int(m.group(1)) >= 2:
            diffs.append(f"经验冲突: 规则标接受应届，JD 实际要求「{e}」")
        elif m and int(m.group(1)) >= 3:
            # 规则没标年限，JD 明确 ≥3 年
            tag_has = any(f"≥{x}" in r for r in rule_exp for x in ("3年", "5年"))
            if not tag_has:
                diffs.append(f"经验漏提: JD 明确「{e}」，规则未标")
    # 4. 其他硬性漏提
    for o in g_other:
        for kw, label in (("英语", "英语要求"), ("出差", "需出差"), ("驻场", "驻场/外包"),
                          ("证书", "证书要求"), ("PMP", "证书要求"), ("软考", "证书要求")):
            if kw in o and label not in rule_other:
                diffs.append(f"其他漏提: JD 要求「{o[:40]}」")
                break
    # 5. 学历规则有但 GLM 明确的（一致性检查）
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    key = load_key()
    con = sqlite3.connect("data/hangzhou_agent.sqlite3")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, company_name, jd_tags_json, job_description FROM jobs "
        "WHERE retention_status = 'active' AND job_description != '' "
        "ORDER BY RANDOM() LIMIT ?", (args.limit,)
    ).fetchall()

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(call_glm, key, r["job_description"]): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            _elapsed, parsed, err = fut.result()
            results.append({"row": r, "parsed": parsed, "err": err})

    total_diff = 0
    for i, x in enumerate(results, 1):
        r = x["row"]
        rule = json.loads(r["jd_tags_json"] or "{}")
        if x["err"] or not x["parsed"]:
            print(f"--- {i}. {r['title'][:22]} [{x['err'] or '解析失败'}]")
            continue
        diffs = compare_one(rule, x["parsed"])
        total_diff += len(diffs)
        print(f"--- {i}. {r['title'][:22]} | {r['company_name'][:12]} | 差异 {len(diffs)}")
        if not diffs:
            print("    无冲突")
        for d in diffs:
            print(f"    ⚠ {d}")
    print(f"\n共 {len(results)} 条，{total_diff} 处差异")

    out = ROOT / "output" / "compare_glm_rule.json"
    out.write_text(json.dumps([
        {"title": x["row"]["title"], "company": x["row"]["company_name"],
         "error": x["err"], "diffs": compare_one(json.loads(x["row"]["jd_tags_json"] or "{}"), x["parsed"]) if x["parsed"] else [],
         "rule_tags": json.loads(x["row"]["jd_tags_json"] or "{}"),
         "glm_tags": x["parsed"]}
        for x in results
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果存: {out}")


if __name__ == "__main__":
    main()
