"""从重标结果里出分析：按岗位类型分层、薪资、技能、公司集中度、标题误导。

只读，不改库。输出一份 JSON，供报告直接引用数字。

## 分层维度

`job_type` 是平台自己标的（0=全职、4=实习、5=校招、6=兼职），比标题可靠；
`role_family` 是决策模型看正文判的，比标题可靠。
两者交叉，才能回答"校招里到底有多少真技术岗"这种问题。

## 标题误导怎么算

不能用模型的 `mismatch` 单独下结论（实测它偏保守，P>=0.5 只占 0.9%）。
更可信的做法是**两条独立路径交叉**：
  路径 A：规则从标题抽岗位族
  路径 B：模型从正文判岗位族
  两者都成立且不同 → 记一次"标题误导"，并保留证据供人工复核。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB = Path(r"D:\Code\Boss\data\hangzhou_agent.sqlite3")
OUT = Path(r"D:\Code\Boss\local\analysis\live-20261018\analysis-report.json")

JOB_TYPE_LABEL = {0: "全职", 4: "实习", 5: "校招/应届", 6: "兼职"}

# 标题 → 岗位族。只用于交叉验证，不参与入库判定。
TITLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ai_app", ("agent", "智能体", "ai应用", "ai 应用", "大模型应用", "llm应用")),
    ("algorithm", ("算法", "机器学习", "深度学习", "nlp", "推荐", "大模型训练")),
    ("data", ("数据仓库", "数仓", "数据分析", "数据开发", "bi", "etl")),
    ("test", ("测试", "qa", "质量")),
    ("ops", ("运维", "sre", "devops", "dba")),
    ("frontend", ("前端", "h5", "小程序", "客户端", "ios", "android")),
    ("fullstack", ("全栈",)),
    ("backend", ("后端", "服务端", "java", "golang", "python开发", "架构")),
    ("product", ("产品经理", "产品")),
    ("operation", ("运营", "增长", "内容", "社群")),
    ("sales", ("销售", "商务", "bd", "客户经理", "渠道")),
    ("design", ("设计", "ui", "ux", "视觉")),
    ("management", ("项目经理", "项目管理", "pmo", "交付")),
]


def title_family(title: str) -> str | None:
    value = (title or "").lower()
    for name, keys in TITLE_RULES:
        if any(key in value for key in keys):
            return name
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(row) for row in db.execute(
        "SELECT canonical_id, title, company_name, city, brand_id, job_type, "
        "salary_min_k, salary_max_k, salary_text, daily_rate, experience, education, "
        "skills_json, is_proxy_job, role_family, role_confidence, title_mismatch, "
        "seniority_score, employment_type, fresh_grad, is_proxy, relabeled_at "
        "FROM jobs WHERE relabeled_at IS NOT NULL AND relabeled_at != '' "
        "AND role_family IS NOT NULL AND role_family != 'error'")]
    db.close()

    report: dict = {"relabeled": len(rows)}
    if not rows:
        print("没有重标数据")
        return 1

    # ---- 按平台岗位类型分层 ----
    by_type: dict[str, dict] = {}
    for value, label in JOB_TYPE_LABEL.items():
        subset = [r for r in rows if r["job_type"] == value]
        if not subset:
            continue
        roles = Counter(r["role_family"] for r in subset)
        tech = sum(count for name, count in roles.items()
                   if name in ("ai_app", "algorithm", "backend", "frontend",
                               "fullstack", "data", "test", "ops"))
        by_type[label] = {
            "count": len(subset),
            "tech_share": round(tech / len(subset), 3),
            "roles": dict(roles.most_common(8)),
        }
    report["by_job_type"] = by_type

    # ---- 总体岗位方向 ----
    report["roles"] = dict(Counter(r["role_family"] for r in rows).most_common())

    # ---- 标题误导（两条独立路径交叉） ----
    comparable = 0
    mismatched = 0
    pairs: Counter = Counter()
    examples: list[dict] = []
    for row in rows:
        from_title = title_family(row["title"])
        if not from_title:
            continue
        comparable += 1
        if from_title == row["role_family"]:
            continue
        # 全栈与前后端/ai_app 属同一大类，不算误导
        if from_title == "fullstack" and row["role_family"] in (
                "backend", "frontend", "ai_app", "fullstack"):
            continue
        mismatched += 1
        pairs[(from_title, row["role_family"])] += 1
        if len(examples) < 60:
            examples.append({"title": row["title"], "company": row["company_name"],
                             "title_family": from_title, "jd_family": row["role_family"],
                             "confidence": row["role_confidence"],
                             "model_mismatch_p": row["title_mismatch"]})
    report["title_vs_jd"] = {
        "comparable": comparable,
        "mismatched": mismatched,
        "rate": round(mismatched / comparable, 4) if comparable else None,
        "top_pairs": [{"title": a, "jd": b, "count": c}
                      for (a, b), c in pairs.most_common(15)],
        "examples": examples[:40],
    }

    # ---- 应届友好度（2026 届最关心） ----
    fresh = Counter(r["fresh_grad"] for r in rows)
    report["fresh_grad"] = dict(fresh.most_common())
    accept = [r for r in rows if r["fresh_grad"] == "accept"]
    report["fresh_grad_accept_by_role"] = dict(
        Counter(r["role_family"] for r in accept).most_common(10))

    # ---- 薪资（按月薪中位，排除日薪岗） ----
    salaries = [(r["salary_min_k"], r["salary_max_k"]) for r in rows
                if r["salary_min_k"] and r["salary_max_k"] and not r["daily_rate"]]
    if salaries:
        mids = sorted((lo + hi) / 2 for lo, hi in salaries)
        report["salary"] = {
            "n": len(mids),
            "median_k": round(mids[len(mids) // 2], 1),
            "p25_k": round(mids[len(mids) // 4], 1),
            "p75_k": round(mids[len(mids) * 3 // 4], 1),
        }
        by_role: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            if row["salary_min_k"] and row["salary_max_k"] and not row["daily_rate"]:
                by_role[row["role_family"]].append(
                    (row["salary_min_k"] + row["salary_max_k"]) / 2)
        report["salary_by_role"] = {
            name: {"n": len(values),
                   "median_k": round(sorted(values)[len(values) // 2], 1)}
            for name, values in sorted(by_role.items(), key=lambda kv: -len(kv[1]))
            if len(values) >= 5}

    # ---- 技能频次 ----
    skills: Counter = Counter()
    for row in rows:
        try:
            values = json.loads(row["skills_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(values, list):
            skills.update(str(v).strip() for v in values if str(v).strip())
    report["skills"] = dict(skills.most_common(30))

    # ---- 公司集中度 ----
    brands = Counter(r["brand_id"] for r in rows if r["brand_id"])
    report["companies"] = {
        "distinct": len(brands),
        "top": [{"brand_id": key, "count": count} for key, count in brands.most_common(15)],
    }

    # ---- 外包/代理 ----
    proxies = [r["is_proxy"] for r in rows if r["is_proxy"] is not None]
    report["proxy"] = {
        "n": len(proxies),
        "flagged_p50": sum(1 for value in proxies if value >= 0.5),
        "mean_p": round(sum(proxies) / len(proxies), 3) if proxies else None,
    }

    # ---- 置信度 ----
    confidences = sorted(r["role_confidence"] for r in rows
                         if r["role_confidence"] is not None)
    if confidences:
        report["confidence"] = {
            "n": len(confidences),
            "median": round(confidences[len(confidences) // 2], 3),
            "below_0_6": sum(1 for value in confidences if value < 0.6),
        }

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"重标样本: {len(rows)}")
    print("\n=== 按平台岗位类型 ===")
    for label, data in by_type.items():
        print(f"  {label:8s} {data['count']:5d}  技术岗占比 {data['tech_share'] * 100:5.1f}%  "
              f"主要方向 {list(data['roles'])[:4]}")
    print("\n=== 岗位方向 ===")
    for name, count in Counter(r["role_family"] for r in rows).most_common(10):
        print(f"  {name:12s} {count:5d}")
    if report["title_vs_jd"]["rate"] is not None:
        print(f"\n=== 标题误导 ===\n  {mismatched}/{comparable} = "
              f"{report['title_vs_jd']['rate'] * 100:.1f}%")
    print(f"\n=== 应届表态 ===\n  {dict(fresh.most_common())}")
    if "salary" in report:
        print(f"\n=== 薪资（月薪 K，n={report['salary']['n']}） ===\n  "
              f"P25={report['salary']['p25_k']} 中位={report['salary']['median_k']} "
              f"P75={report['salary']['p75_k']}")
    print(f"\n输出: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
