"""用 patchright launch_persistent_context 启动浏览器，验证路径 1。

复用 .browser-profile（登录态保留在里），打开 BOSS 搜索页。
观察：能否正常启动、是否触发刷新循环、登录态是否保留。
浏览器窗口保持打开 60 秒供观察，之后关闭。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"


async def main() -> None:
    print("patchright launch_persistent_context 启动中（复用 .browser-profile）...")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print(f"启动成功，打开 BOSS 搜索页 ...")
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(3000)
        print(f"当前 URL: {page.url[:90]}")

        # 观察 15s 内 URL 是否稳定（不刷新循环）
        urls = set()
        for _ in range(10):
            try:
                urls.add(page.url)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
        print(f"15s 内出现 {len(urls)} 个不同 URL")
        for u in sorted(urls):
            print(f"  - {u[:85]}")

        # 读登录态
        try:
            info = await page.evaluate("""() => {
                const p = window._PAGE || {};
                return {
                    hasPage: typeof window._PAGE !== 'undefined',
                    token: (p.token || '').slice(0, 24),
                    identity: p.identity,
                };
            }""")
            print(f"登录态: {info}")
        except Exception as exc:
            print(f"读登录态失败: {exc}")

        print("浏览器窗口保持打开 60 秒供观察...")
        await page.wait_for_timeout(60_000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
