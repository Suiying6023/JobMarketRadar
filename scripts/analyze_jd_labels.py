"""统计 JD 正文里的学历/经验表述，和平台标签做对照。

纯读库，零网络请求。目的是在写提取规则之前先看清真实文本长什么样：
- 平台标签（education / experience 字段）和 JD 正文说的是否一致
- JD 里的表述有哪些写法，够不够用正则覆盖

用法: python scripts/analyze_jd_labels.py
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

# 先用宽松模式做"探查"，不做判定，只为看清写法分布。
DEGREE_PROBE = re.compile(r"[^。；;\n]{0,24}(?:大专|专科|本科|硕士|研究生|博士|学历)[^。；;\n]{0,24}")
EXP_PROBE = re.compile(r"[^。；;\n]{0,20}(?:\d+\s*年|经验不限|应届|在校)[^。；;\n]{0,20}")


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, education, experience, job_description FROM jobs "
        "WHERE job_description != ''"
    ).fetchall()
    con.close()
    print(f"有 JD 的记录: {len(rows)}\n")

    print("--- 平台 education 取值分布 ---")
    for value, n in Counter(r["education"] or "(空)" for r in rows).most_common():
        print(f"  {n:4d}  {value}")

    print("\n--- 平台 experience 取值分布 ---")
    for value, n in Counter(r["experience"] or "(空)" for r in rows).most_common():
        print(f"  {n:4d}  {value}")

    deg_hits = 0
    exp_hits = 0
    deg_samples: list[str] = []
    exp_samples: list[str] = []
    for r in rows:
        jd = r["job_description"] or ""
        d = DEGREE_PROBE.findall(jd)
        e = EXP_PROBE.findall(jd)
        if d:
            deg_hits += 1
            if len(deg_samples) < 25:
                deg_samples.append(f"[{r['education']}] {d[0].strip()}")
        if e:
            exp_hits += 1
            if len(exp_samples) < 25:
                exp_samples.append(f"[{r['experience']}] {e[0].strip()}")

    print(f"\nJD 里提到学历的: {deg_hits}/{len(rows)}")
    print(f"JD 里提到经验的: {exp_hits}/{len(rows)}")

    print("\n--- 学历表述样本（[平台标签] JD片段）---")
    for s in deg_samples:
        print(f"  {s}")

    print("\n--- 经验表述样本（[平台标签] JD片段）---")
    for s in exp_samples:
        print(f"  {s}")


if __name__ == "__main__":
    main()
