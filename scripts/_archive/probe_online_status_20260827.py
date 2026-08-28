"""一次性探针：确认经波尔 SOCKS 出口、免登录能从公开详情页读到哪些状态字段。

只读不写库。复用 check_job_status.py 的既定做法：隐身上下文（无 cookie）、
等 BOSS 的 JS 挑战页自解（标题不再是「请稍候」），然后读整页 innerText。

用法（先起隧道，注意选对机器）：
    ssh -f -N -D 1081 bohrium-container    # 这台可用
    uv run python scripts/probe_online_status.py

⚠️ 选机器的判据是 BOSS 首页返回码，一条 curl 即可，不必起浏览器：
    curl -sS --socks5-hostname 127.0.0.1:<port> -o /dev/null -w "%{http_code}" https://www.zhipin.com/
  200 = 该 IP 未被标记，挑战可自解；302 = 已被标记，会落到需点击验证的「安全验证」页。
  2026-08-27 实测：bohrium-container 200 可用，bohrium-vm 302 已被标记。

⚠️ 已知竞态：title 变正常后 body.innerText 仍可能是空的（实测第一条 len=0、
  第二条 1655）。正式跑批要在标题通过后再等 DOM，或改用 wait_for_selector。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
SOCKS = "socks5://127.0.0.1:1080"

# 库里 recruiter_active_status 出现过的文案，用来确认页面是否渲染活跃度
ACTIVE_HINTS = ("在线", "刚刚活跃", "今日活跃", "本周活跃", "半年前活跃",
                "月内活跃", "日内活跃", "活跃")
CLOSED_HINTS = ("职位已关闭", "该职位已关闭", "已下线", "职位已结束", "停止招聘")


async def probe(page, job_id: str, max_wait: float = 15.0) -> dict:
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    out = {"job_id": job_id, "title": None, "len": 0, "text": "", "solved": False}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        out["error"] = f"goto: {exc}"
        return out

    loop = asyncio.get_event_loop()
    start = loop.time()
    while loop.time() - start < max_wait:
        try:
            title = await page.title()
        except Exception as exc:
            out["error"] = f"title: {exc}"
            return out
        out["title"] = title
        if "请稍候" not in title and "验证" not in title and title.strip():
            out["solved"] = True
            break
        await asyncio.sleep(1.5)

    try:
        out["text"] = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception as exc:
        out["error"] = f"evaluate: {exc}"
    out["len"] = len(out["text"])
    return out


async def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_job_id, title, company_name, recruiter_active_status, "
        "       recruiter_online, job_invalid, job_status_desc "
        "FROM jobs WHERE retention_status='active' AND job_invalid=0 "
        "  AND recruiter_active_status IS NOT NULL "
        "ORDER BY canonical_id LIMIT 4"
    ).fetchall()
    if not rows:
        print("没有可探测的样本")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        # 隐身上下文 + 波尔 SOCKS 出口，绝不带主账号 profile
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            proxy={"server": SOCKS})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for r in rows:
            print("=" * 72)
            print(f"库里记录: {r['title']} @ {r['company_name']}")
            print(f"  active_status={r['recruiter_active_status']!r} "
                  f"online={r['recruiter_online']} invalid={r['job_invalid']} "
                  f"status_desc={r['job_status_desc']!r}")
            res = await probe(page, r["source_job_id"])
            print(f"  页面标题: {res['title']!r}  挑战已过={res['solved']}  "
                  f"innerText 长度={res['len']}")
            if res.get("error"):
                print(f"  ERROR: {res['error']}")
            text = res["text"]
            if not text:
                print("  空页面，无法判定")
                continue

            hit_active = [h for h in ACTIVE_HINTS if h in text]
            hit_closed = [h for h in CLOSED_HINTS if h in text]
            print(f"  活跃度关键词命中: {hit_active}")
            print(f"  关闭关键词命中:   {hit_closed}")

            # 把活跃度文案的上下文打出来，确认它是否可稳定解析
            for h in ("活跃", "在线"):
                for m in re.finditer(re.escape(h), text):
                    lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
                    frag = text[lo:hi].replace("\n", " | ")
                    print(f"    …{frag}…")
                    break

            print("  ---- innerText 前 900 字 ----")
            print("  " + text[:900].replace("\n", "\n  "))
            await asyncio.sleep(3)

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    # 不要设 WindowsSelectorEventLoopPolicy：selector 循环在 Windows 上不支持
    # 子进程，而 Playwright 要 spawn driver（NotImplementedError）。默认的
    # Proactor 循环才行——check_job_status.py 同样什么都不设。
    asyncio.run(main())
