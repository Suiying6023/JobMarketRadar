"""popomv.me 登录 + 完整探索（同一会话内）。

登录后 dump 导航菜单全项 + 更新日志内容，找「招聘」功能入口。只观察，不批量请求。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")
ACCOUNT = "Suiying"
PASSWORD = "GXH6023GXH"


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
        if "popomv.me/api" in url and url not in seen:
            seen.add(url)
            print(f"[API] {req.method} {url[:160]}", flush=True)

    page.on("request", on_req)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        # 登录
        try:
            await page.click("text=立即登录", timeout=4000)
            await page.wait_for_timeout(800)
            await page.fill('input[placeholder*="用户名"]', ACCOUNT)
            await page.fill('input[placeholder*="密码"]', PASSWORD)
            await page.evaluate(
                """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
            )
            await page.wait_for_timeout(3500)
        except Exception as e:
            print("登录:", type(e).__name__, str(e)[:120], flush=True)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print("=== 顶部/菜单区域（前 60 个短文本元素）===")
        items = await page.evaluate(
            """() => [...document.querySelectorAll('div,span,a,li,button')]
                .filter(e => e.innerText && e.innerText.trim().length <= 10)
                .map(e => e.innerText.trim()).filter((t,i,a)=>t && a.indexOf(t)===i)"""
        )
        for t in items[:60]:
            print(f"  {t}", flush=True)
        print("=== 更新日志（点开）===")
        try:
            await page.click("text=更新日志", timeout=4000)
            await page.wait_for_timeout(2000)
            logtext = await page.evaluate("document.body ? document.body.innerText : ''")
            # 找日志区域
            i = logtext.find("更新日志")
            print(logtext[i:i+1500].replace(chr(10), " ⏎ ") if i >= 0 else logtext[:600], flush=True)
        except Exception as e:
            print("日志:", type(e).__name__, str(e)[:120], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
