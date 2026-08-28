"""patchright 路径登录态确认：等待 _PAGE 完全初始化后读登录信息。

上次读太早（_security_check 跳转中 _PAGE 还没建）。这次轮询等 _PAGE.token
出现再读，最多等 30 秒。同时验证页面内 fetch 是否能跑（这是采集的核心操作）。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    print("启动 patchright persistent 浏览器（复用 .browser-profile）...")
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
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=30_000,
        )

        # 轮询等 _PAGE 初始化（最多 30s），期间观察 URL 稳定性
        token = ""
        stable_urls = set()
        for i in range(20):
            try:
                stable_urls.add(page.url)
                token = str(await page.evaluate(
                    "() => (window._PAGE && window._PAGE.token) || ''"))
            except Exception:
                token = ""
            if token:
                break
            await page.wait_for_timeout(1500)
        print(f"等 _PAGE 初始化用了 {i+1} 次轮询")
        print(f"观察到的 URL 数: {len(stable_urls)}（>1 = 有刷新循环）")
        for u in sorted(stable_urls):
            print(f"  - {u[:85]}")

        if token:
            info = await page.evaluate("""() => {
                const p = window._PAGE || {};
                return {
                    userId: (p.userId || '').toString().slice(0, 12),
                    name: p.name || '',
                    token: (p.token || '').slice(0, 24),
                };
            }""")
            print(f"\n登录态: {info}")

            # 验证页面内 fetch 能跑（采集核心操作）
            result = await page.evaluate("""async () => {
                try {
                    const r = await fetch('/wapi/zpuser/wap/getUserInfo.json', {
                        credentials: 'include',
                        'Accept': 'application/json, text/plain, */*',
                        'X-Requested-With': 'XMLHttpRequest',
                        'token': window._PAGE.token,
                    });
                    return {status: r.status, text: await r.text()};
                } catch (e) {
                    return {status: -1, error: String(e)};
                }
            }""")
            print(f"\n页面内 fetch getUserInfo: HTTP {result.get('status')}")
            text = result.get("text") or ""
            print(f"  响应: {text[:200]}")
        else:
            print("\n!! 30 秒内 _PAGE.token 一直没出现，登录态可能丢了")
            print(f"当前 URL: {page.url[:90]}")

        print("\n浏览器保持打开 20 秒观察...")
        await page.wait_for_timeout(20_000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
