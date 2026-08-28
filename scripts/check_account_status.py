"""前台检查账号状态：打开搜索页，读登录态/封禁/验证页。

用法: python scripts/check_account_status.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    print("前台打开浏览器检查账号状态...", flush=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        # 等页面稳定（_security_check 跳转 / 封禁页跳转）
        for _ in range(10):
            await page.wait_for_timeout(1500)
            try:
                if "jobs" in page.url or "403" in page.url or "passport" in page.url or "verify" in page.url:
                    break
            except Exception:
                pass

        url = page.url
        print(f"\n当前 URL: {url[:100]}")

        # 判定页面类型
        status = "正常搜索页"
        if "403" in url:
            status = "🔴 封禁页(403)"
        elif "passport" in url:
            status = "🔴 登录/认证页"
        elif "verify" in url or "security" in url:
            status = "🟡 验证页"
        print(f"页面状态: {status}")

        # 读登录态
        try:
            info = await page.evaluate("""() => {
                const p = window._PAGE || {};
                return {
                    hasPage: typeof window._PAGE !== 'undefined',
                    token: (p.token || '').slice(0, 16),
                    userId: (p.userId || '').toString().slice(0, 12),
                    name: p.name || '',
                };
            }""")
            print(f"登录态: {info}")
        except Exception as exc:
            print(f"读登录态失败: {exc}")

        # 读页面正文开头（看是否封禁文案）
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''")
            print(f"\n页面正文前 300 字:\n{text}")
        except Exception as exc:
            print(f"读正文失败: {exc}")

        # 保持窗口打开 30 秒供观察
        print("\n浏览器保持打开 30 秒供观察...")
        await page.wait_for_timeout(30_000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
