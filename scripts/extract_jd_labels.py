"""从 JD 正文提取结构化门槛标签（经验/学历院校/技术栈/其他要求）。

针对投递池（active + 已补JD，即 P0 优先 + P1 待确认）的岗位详细内容，
识别 JD 正文里的隐含要求。区分「硬性要求」与「加分项」（优先/加分字样的上下文）。

标签维度：
- exp   经验要求（接受应届 / N年以上 / 经验不限）
- edu   学历院校（本科+ / 硕士+ / 博士 / 名校985211 / 全日制 / 留学）
- tech  硬性技术栈（语言 / 大模型 / 框架）
- other 其他门槛（英语 / 出差 / 驻场 / 证书 / 到岗时间）

用法:
  python scripts/extract_jd_labels.py            # 只打印分布与样例，不写库
  python scripts/extract_jd_labels.py --write     # 写回 DB(jd_tags_json) + 导出 CSV
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.storage import JobStore  # noqa: E402

BONUS_MARKERS = ["优先", "加分", "加分项", "优先考虑", "优先录用", "更佳"]
# 硬性要求常出现在这些小节标题里
REQ_SECTION = re.compile(r"(任职要求|岗位要求|任职资格|基本要求|职位要求|我们需要|希望你|职位亮点后)")

# (标签, 正则) —— 命中即打标签
EXP_RULES: list[tuple[str, re.Pattern]] = [
    ("接受应届/无经验", re.compile(r"应届|无经验|毕业[一二1-2]\s*年?内|在校生可|实习生也")),
    ("经验不限", re.compile(r"经验不限|不限经验|无年限")),
    ("N年以上", re.compile(r"(\d+)\s*[个]?\s*年(?:以上|及以下|以上经验|以上工作|以上相关)")),
]
EDU_RULES: list[tuple[str, re.Pattern]] = [
    ("本科+", re.compile(r"本科及以上|本科以上|本科学历")),
    ("硕士+", re.compile(r"硕士及以上|硕士研究生|研究生学历|硕士学历")),
    ("博士", re.compile(r"博士")),
    ("名校985/211", re.compile(r"985|211|双一流|重点高校|重点院校|知名院校|世界名校")),
    ("全日制", re.compile(r"全日制")),
    ("留学背景", re.compile(r"海归|留学|海外学历|海外背景")),
]
TECH_LANG = [("Python", "Python"), ("Java", "Java"), ("C++", r"C\+\+"),
             ("Go", r"\bGo\b"), ("JavaScript/TS", r"JavaScript|TypeScript"),
             ("前端", r"前端|Web开发|Vue|React")]
TECH_AI = [("大模型", r"大模型|LLM|Transformer|GPT|语言模型|多模态"),
           ("RAG", r"RAG|检索增强|向量数据库|知识库"),
           ("Agent开发", r"Agent|智能体|Function.?Calling|Tool.?Use|LangChain|Dify|Coze"),
           ("PyTorch/TF", r"PyTorch|TensorFlow|深度学习框架"),
           ("分布式/微服务", r"分布式|微服务|Kubernetes|k8s|高并发|Spring.?Cloud")]
OTHER_RULES: list[tuple[str, re.Pattern]] = [
    ("英语要求", re.compile(r"英语|CET-?[46]|六级|四级|English|英文读写")),
    ("需出差", re.compile(r"出差")),
    ("驻场/外包", re.compile(r"驻场|外包项目|人力外包")),
    ("证书要求", re.compile(r"证书|PMP|软考|CKA|认证")),
    ("尽快到岗", re.compile(r"尽快到岗|立即到岗|随时到岗|一周内到岗|尽快入职")),
    ("抗压/加班", re.compile(r"抗压|加班|高压环境|高强度")),
]


def hit_bonus(text: str, start: int, end: int) -> bool:
    """关键词前 40 字内出现「优先/加分」视为加分项，不是硬性门槛。"""
    window = text[max(0, start - 40):end]
    return any(m in window for m in BONUS_MARKERS)


def extract_tags(jd: str) -> dict:
    text = jd or ""
    tags = {"exp": [], "edu": [], "tech": [], "other": [], "bonus": []}
    for label, pat in EXP_RULES:
        for m in pat.finditer(text):
            if label == "N年以上":
                years = m.group(1) if m.groups() else ""
                tag = f"经验≥{years}年" if years else "有经验要求"
                if hit_bonus(text, m.start(), m.end()):
                    tags["bonus"].append(f"{tag}(加分)")
                elif tag not in tags["exp"]:
                    tags["exp"].append(tag)
            elif label not in tags["exp"]:
                tags["exp"].append(label)
    for label, pat in EDU_RULES:
        if pat.search(text):
            if hit_bonus(text, pat.search(text).start(), pat.search(text).end()):
                tags["bonus"].append(label)
            else:
                tags["edu"].append(label)
    # 技术栈：只在出现时打标签（语言与大模型分开计数）
    found_lang = [label for label, pat in TECH_LANG if re.search(pat, text)]
    found_ai = [label for label, pat in TECH_AI if re.search(pat, text)]
    tags["tech"] = found_lang[:3] + found_ai[:4]
    for label, pat in OTHER_RULES:
        m = pat.search(text)
        if m:
            if hit_bonus(text, m.start(), m.end()):
                tags["bonus"].append(label)
            else:
                tags["other"].append(label)
    # 去重保序
    for k in tags:
        seen: set[str] = set()
        tags[k] = [v for v in tags[k] if not (v in seen or seen.add(v))]
    return tags


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    parser.add_argument("--write", action="store_true", help="写回 DB + 导出 CSV（默认只预览）")
    parser.add_argument("--limit", type=int, default=0, help="处理条数上限（0=全部）")
    args = parser.parse_args()

    with JobStore(args.db) as store:
        rows = store.conn.execute(
            "SELECT * FROM jobs WHERE retention_status = 'active' AND job_description != ''"
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        print(f"处理 {len(rows)} 条投递池岗位\n")

        results = []
        tag_counter: dict[str, dict[str, int]] = {"exp": {}, "edu": {}, "tech": {}, "other": {}, "bonus": {}}
        for r in rows:
            tags = extract_tags(r["job_description"])
            results.append((r, tags))
            for cat, items in tags.items():
                for t in items:
                    tag_counter[cat][t] = tag_counter[cat].get(t, 0) + 1

        for cat, label in [("exp", "经验要求"), ("edu", "学历/院校"), ("tech", "技术栈"), ("other", "其他门槛"), ("bonus", "加分项")]:
            if not tag_counter[cat]:
                continue
            print(f"=== {label} 分布 ===")
            for t, n in sorted(tag_counter[cat].items(), key=lambda x: -x[1]):
                print(f"  {t}: {n}")
            print()

        # 无任何标签的比例
        empty = sum(1 for _r, t in results if not any(t.values()))
        print(f"无任何可提取标签: {empty}/{len(results)}")

        if args.write:
            with JobStore(args.db) as store2:
                n = 0
                for r, tags in results:
                    store2.conn.execute(
                        "UPDATE jobs SET jd_tags_json = ? WHERE canonical_id = ?",
                        (json.dumps(tags, ensure_ascii=False), r["canonical_id"]),
                    )
                    n += 1
                store2.conn.commit()
                print(f"\n已写回 DB: {n} 条")
                # 导出增强 CSV
                import csv as _csv
                from job_radar.collectors.boss import RISK_CODES  # noqa: F401
                p0 = [r for r in rows if r["recruiter_active_status"] in
                      ("刚刚活跃", "在线", "今日活跃", "3日内活跃")]
                p1 = [r for r in rows if r["recruiter_active_status"] == ""]
                for name, subset in (("投递池_P0优先_标签", p0), ("投递池_P1待确认_标签", p1)):
                    with open(f"output/{name}.csv", "w", encoding="utf-8-sig", newline="") as f:
                        w = _csv.DictWriter(f, fieldnames=[
                            "title", "company_name", "district", "salary_text", "experience",
                            "education", "recruiter_active_status", "company_size",
                            "financing_stage", "industry", "exp_tag", "edu_tag",
                            "tech_tag", "other_tag", "bonus_tag", "job_url"])
                        w.writeheader()
                        for r in subset:
                            tags = dict(next(t for rr, t in results if rr["canonical_id"] == r["canonical_id"]))
                            w.writerow({
                                "title": r["title"], "company_name": r["company_name"],
                                "district": r["district"], "salary_text": r["salary_text"],
                                "experience": r["experience"], "education": r["education"],
                                "recruiter_active_status": r["recruiter_active_status"],
                                "company_size": r["company_size"], "financing_stage": r["financing_stage"],
                                "industry": r["industry"],
                                "exp_tag": "/".join(tags["exp"]), "edu_tag": "/".join(tags["edu"]),
                                "tech_tag": "/".join(tags["tech"]), "other_tag": "/".join(tags["other"]),
                                "bonus_tag": "/".join(tags["bonus"]), "job_url": r["job_url"],
                            })
                    print(f"已导出 output/{name}.csv: {len(subset)} 条")


if __name__ == "__main__":
    main()
