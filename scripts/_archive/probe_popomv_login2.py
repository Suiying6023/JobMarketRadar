"""popomv.me 登录 + 找招聘信息入口。

登录后 dump 菜单/页面文本，捕获登录和后续加载的 API。只做必要的几步，不批量请求。
"""
from __future__ import annotations

import asyncio
import json
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
    bodies: dict[str, str] = {}

    def on_req(req):
        url = req.url
        if "popomv.me/api" in url:
            if url not in seen:
                seen.add(url)
                post = ""
                if req.method == "POST":
                    try:
                        post = f" body={req.post_data[:150] if req.post_data else ''}"
                    except Exception:
                        pass
                print(f"[API] {req.method} {url[:170]}{post}", flush=True)

    def on_resp(resp):
        url = resp.url
        if "popomv.me/api" in url and resp.request.method == "POST":
            try:
                body = resp.text()
                bodies[url] = body[:300]
            except Exception:
                pass

    page.on("request", on_req)
    page.on("response", on_resp)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.click("text=立即登录", timeout=5000)
        await page.wait_for_timeout(1000)
        await page.fill('input[placeholder*="用户名"]', ACCOUNT)
        await page.fill('input[placeholder*="密码"]', PASSWORD)
        await page.evaluate(
            """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
        )
        await page.wait_for_timeout(4000)
        print("=== 登录后页面文本 ===")
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print(text[:1500].replace(chr(10), " ⏎ "), flush=True)
        print("=== 菜单/链接 ===")
        links = await page.evaluate(
            """() => [...document.querySelectorAll('a')].slice(0,40).map(a=>({t:(a.innerText||'').trim(), h:a.href})).filter(x=>x.t)"""
        )
        for l in links:
            print(f"  {l['t'][:24]:26} -> {l['h'][:80]}", flush=True)
        if bodies:
            print("=== 登录响应 ===")
            for u, b in bodies.items():
                print(f"  {u[:100]}: {b[:200]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    # 保留 profile 以便后续步骤复用登录态
    print("PROFILE_KEEP", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
