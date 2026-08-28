"""popomv.me 登录流程探测：点击「立即登录」，dump 表单字段 + 捕获登录相关 API。

只做登录表单的观察，不批量请求。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")


async def main() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
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
        if "popomv.me" in url and req.resource_type in ("xhr", "fetch"):
            if url not in seen:
                seen.add(url)
                print(f"[API] {req.method} {url[:180]}", flush=True)

    page.on("request", on_req)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        # 点「立即登录」
        try:
            await page.click("text=立即登录", timeout=5000)
            await page.wait_for_timeout(2500)
        except Exception as e:
            print("点登录按钮:", type(e).__name__, str(e)[:100], flush=True)
        print("=== 页面文本 ===")
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print(text[:800].replace(chr(10), " ⏎ "), flush=True)
        print("=== 输入框 ===")
        inputs = await page.query_selector_all("input")
        for i, el in enumerate(inputs):
            info = await el.evaluate("e => ({t:e.type||'', n:e.name||'', i:e.id||'', p:e.placeholder||''})")
            print(f"  [{i}] {info}", flush=True)
        # 登录按钮
        btns = await page.query_selector_all("button")
        for b in btns[:8]:
            txt = (await b.inner_text()).strip()
            if txt:
                print(f"  btn: {txt[:30]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
