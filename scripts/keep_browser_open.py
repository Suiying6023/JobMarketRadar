"""打开 BOSS 搜索页并保持浏览器窗口打开（供用户手动操作梳理筛选标签）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

PROFILE = Path("D:/Code/Boss/.browser-profile")
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    print("打开 BOSS 搜索页，保持窗口打开 ...")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)
        print(f"已打开: {page.url[:90]}")
        # 保持打开直到 Ctrl+C
        try:
            while True:
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
