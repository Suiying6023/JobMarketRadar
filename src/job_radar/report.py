from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .analysis.classifier import classify, extract_skills
from .analysis.market import connect_readonly
from .analysis.decisions import safe_job_url


def generate_html_report(database: str, output: str) -> Path:
    conn = connect_readonly(database)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM jobs ORDER BY city, company_name, title"))
    conn.close()

    city_counter = Counter(row["city"] or "未知" for row in rows)
    category_counter: Counter[str] = Counter()
    skill_counter: Counter[str] = Counter()
    for row in rows:
        category_counter.update(classify(row["title"], row["job_description"]))
        skill_counter.update(extract_skills(f"{row['title']}\n{row['job_description']}"))

    def list_items(counter: Counter[str], limit: int = 20) -> str:
        return "".join(
            f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in counter.most_common(limit)
        )

    def job_link(value: str) -> str:
        url = safe_job_url(value)
        return f"<a href='{html.escape(url)}' rel='noopener noreferrer'>原链接</a>" if url else "链接不可用"

    rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(row['city'])}</td>"
        f"<td>{html.escape(row['title'])}</td>"
        f"<td>{html.escape(row['company_name'])}</td>"
        f"<td>{html.escape(row['salary_text'])}</td>"
        f"<td>{html.escape(row['experience'])}</td>"
        f"<td>{html.escape(row['education'])}</td>"
        f"<td>{job_link(row['job_url'])}</td>"
        "</tr>"
        for row in rows[:500]
    )

    document = f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><title>AI 岗位市场报告</title>
<style>
body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:32px;line-height:1.5;color:#222}}
h1,h2{{margin-top:28px}} table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}} th{{background:#f5f5f5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}}
.card{{border:1px solid #ddd;border-radius:10px;padding:16px}} .muted{{color:#666}}
</style></head><body>
<h1>本地岗位采样报告</h1>
<p class='muted'>记录数：{len(rows)}。该报告反映采集时点的搜索结果样本，不代表平台完整库存。</p>
<div class='grid'>
<div class='card'><h2>城市岗位数</h2><table><tr><th>城市</th><th>岗位</th></tr>{list_items(city_counter)}</table></div>
<div class='card'><h2>岗位方向</h2><table><tr><th>方向</th><th>岗位</th></tr>{list_items(category_counter)}</table></div>
<div class='card'><h2>技术词频</h2><table><tr><th>技能</th><th>岗位</th></tr>{list_items(skill_counter)}</table></div>
</div>
<h2>岗位明细（最多 500 条）</h2>
<table><tr><th>城市</th><th>岗位</th><th>公司</th><th>薪资</th><th>经验</th><th>学历</th><th>链接</th></tr>{rows_html}</table>
</body></html>"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
