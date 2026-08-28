"""popomv.me 已登录态下找「招聘信息」入口。

复用 .tmp-popomv-profile 登录态，搜索含「招聘」的元素 + dump 导航结构 + 捕获 API。
只观察，不批量请求。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")


async def main() -> None:
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=False, no_viewport=True,
        args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
        ignore_default_args=["--enable-automation"],
    )
    page = await ctx.new_page()
    seen: set[str] = set()

    def on_req(req):
        url = req.url
        if "popomv.me/api" in url and url not in seen:
            seen.add(url)
            print(f"[API] {req.method} {url[:170]}", flush=True)

    page.on("request", on_req)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print("=== 含「招聘」的文本片段 ===")
        i = text.find("招聘")
        while i >= 0:
            print("  ...", text[max(0, i-40):i+40].replace(chr(10), " "), flush=True)
            i = text.find("招聘", i+1)
            if i > 8000:
                break
        print("=== 导航/菜单可点击项 ===")
        items = await page.evaluate(
            """() => [...document.querySelectorAll('div,span,a,li,button')]
                .filter(e => e.innerText && e.innerText.trim().length <= 8)
                .map(e => ({t: e.innerText.trim(), cls: e.className || ''}))
                .filter(x => x.t)"""
        )
        seen_items = {}
        for it in items:
            key = it["t"]
            if key not in seen_items:
                seen_items[key] = it["cls"][:40]
        for t, c in list(seen_items.items())[:60]:
            print(f"  {t:20} | {c}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
