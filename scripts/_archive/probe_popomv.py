"""低调探测 popomv.me 的数据源接口。

只做 1 次页面加载 + 抓取 XHR/fetch 请求，绝不批量请求/不给站点加压。
识别招聘信息等数据是从哪个 API 来的。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")
SKIP = (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2", ".gif", ".ico", ".webp")


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
        if req.resource_type in ("xhr", "fetch") and not any(url.endswith(s) for s in SKIP):
            if url not in seen:
                seen.add(url)
                print(f"[API] {url[:200]}", flush=True)

    page.on("request", on_req)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)
        print("最终URL:", page.url[:120], flush=True)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        print("=== 页面文本前 1200 字 ===")
        print(text[:1200].replace(chr(10), " ⏎ "), flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
