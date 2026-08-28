"""popomv.me：点开公司详情页，找「招聘」数据 + 更新日志。

同一会话内登录 → 点最新注册的第一家公司 → dump 详情页菜单/文本/API → 找招聘信息。
只做必要点击，不批量请求。
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
        await page.click("text=立即登录", timeout=4000)
        await page.wait_for_timeout(800)
        await page.fill('input[placeholder*="用户名"]', ACCOUNT)
        await page.fill('input[placeholder*="密码"]', PASSWORD)
        await page.evaluate(
            """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
        )
        await page.wait_for_timeout(3500)
        # 点第一家公司
        clicked = await page.evaluate(
            """() => { const el=[...document.querySelectorAll('div,span,a,li')]
                .find(e=>{const t=(e.innerText||'').trim(); return /有限公司$/.test(t) && t.length<30;});
                if(el){el.click(); return el.innerText.trim();} return ''; }"""
        )
        print(f"点击公司: {clicked}", flush=True)
        await page.wait_for_timeout(4000)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print("=== 公司详情页文本（前 1500）===")
        print(text[:1500].replace(chr(10), " ⏎ "), flush=True)
        print("=== 含「招聘/职位/招聘信息」 ===")
        for kw in ("招聘", "职位"):
            i = text.find(kw)
            if i >= 0:
                print(f"  [{kw}]", text[max(0, i-50):i+60].replace(chr(10), " "), flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
