"""抽查多个岗位：对比页面渲染 JD 与存库 postDescription，找结构化字段盲区。

每个岗位 1 次页面导航，共 5 次，克制。郭辉 profile。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from job_radar.collectors.browser import BrowserSession  # noqa: E402
from job_radar.config import load_config  # noqa: E402

JOBS = [
    ("c0601cf916420a410nF-2dW0GFJU", "https://www.zhipin.com/job_detail/c0601cf916420a410nF-2dW0GFJU.html", "多益 AI应用开发27届校招"),
    ("0da5f93e1e1df95e0nBy3d69GFBS", "https://www.zhipin.com/job_detail/0da5f93e1e1df95e0nBy3d69GFBS.html", "浙大 智能体研发应届"),
    ("c7bc9c62646f87810nJ72dy9GVNZ", "https://www.zhipin.com/job_detail/c7bc9c62646f87810nJ72dy9GVNZ.html", "无际云帆 ai智能体开发"),
    ("f21f8d7fe52f89da03x73t2_E1VW", "https://www.zhipin.com/job_detail/f21f8d7fe52f89da03x73t2_E1VW.html", "玄材 AI智能体开发"),
    ("2ec312908772991e0nF539u9E1VV", "https://www.zhipin.com/job_detail/2ec312908772991e0nF539u9E1VV.html", "威匡匡 大模型应用开发"),
]
STRUCT_LABELS = ["毕业时间", "招聘截止", "截止日期", "招聘对象", "开始时间", "结束时间", "实习期", "招聘人数", "到岗时间"]
import sqlite3, json


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    sess = BrowserSession(cfg.browser)
    ctx = await sess.start()
    con = sqlite3.connect("data/hangzhou_agent.sqlite3")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        for jid, url, label in JOBS:
            stored = cur.execute("SELECT job_description FROM jobs WHERE source_job_id=?", (jid,)).fetchone()
            stored_jd = (stored["job_description"] or "") if stored else ""
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(4000)
                text = await page.evaluate("document.body ? document.body.innerText : ''")
                idx = text.find("职位描述")
                region = text[idx:idx+2500] if idx >= 0 else ""
                # 结构化字段检测
                found = [l for l in STRUCT_LABELS if l in region]
                # 页面 JD 里 岗位职责 起的正文
                jdx = region.find("岗位职责")
                page_jd = region[jdx:jdx+2000] if jdx >= 0 else region[:2000]
                # 对比页面 JD 长度 vs 存储
                overlap = sum(1 for _ in range(min(len(stored_jd), len(page_jd))) if stored_jd[_] == page_jd[_])
                print(f"\n[{label}]", flush=True)
                print(f"  存库 JD {len(stored_jd)} 字 | 页面 JD ~{len(page_jd)} 字 | 开头匹配 {overlap} 字", flush=True)
                if found:
                    print(f"  ⚠️ 页面结构化字段(不在postDescription): {found}", flush=True)
                    for l in found:
                        i = region.find(l)
                        print(f"    {region[max(0,i-15):i+60].replace(chr(10),' ')}", flush=True)
                else:
                    print("  ✓ 无毕业时间/截止等结构化字段盲区", flush=True)
            except Exception as e:
                print(f"\n[{label}] ERR {type(e).__name__} {str(e)[:80]}", flush=True)
            finally:
                await page.close()
    finally:
        await sess.close()
        con.close()


if __name__ == "__main__":
    asyncio.run(main())
