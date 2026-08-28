"""打开搜索页并持续记录 URL 变化（供用户手动筛选时抓取真实参数码）。

用户手动在浏览器里点选筛选，BOSS 是 SPA，选中后 URL 的 query 会更新
（salary=..&experience=.. 等），本脚本每 2 秒记录一次 URL 变化，
把每次变化的时间戳和新 URL 打印出来，供分析真实筛选码。

用法: python scripts/watch_filter_url.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

PROFILE = Path("D:/Code/Boss/.browser-profile")
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    print("打开搜索页并监听 URL 变化（操作筛选时 URL 会更新）...", flush=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 初始 URL: {page.url}", flush=True)

        last = page.url
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    cur = page.url
                except Exception:
                    continue
                if cur != last:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] URL 变化:", flush=True)
                    print(f"   {cur}", flush=True)
                    last = cur
        except asyncio.CancelledError:
            pass
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
