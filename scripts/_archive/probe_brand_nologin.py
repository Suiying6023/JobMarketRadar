"""测试 gongsi 页工商信息是否需要登录。

用全新临时 profile（无任何登录态）打开 gongsi 页，若工商信息仍渲染，
则富化可走干净浏览器、完全不用郭辉账号，零账号风险。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

BRAND_ID = "59f3e5a3ba3d0ef20Hd639u0Fg~~"  # 聚能视界
GONGSI = f"https://www.zhipin.com/gongsi/{BRAND_ID}.html"
PROFILE = Path(".tmp-probe-profile")
KW = ["注册资本", "法定代表人", "成立时间", "企业类型", "经营状态", "统一社会信用代码"]


async def main() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(PROFILE),
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
        ignore_default_args=["--enable-automation"],
    )
    page = await ctx.new_page()
    try:
        await page.goto(GONGSI, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(7000)
        print("最终URL:", page.url[:100], flush=True)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        for kw in KW:
            i = text.find(kw)
            if i >= 0:
                print(f"[DOM] {kw}: {text[i:i+60]!r}", flush=True)
        if not any(text.find(k) >= 0 for k in ["注册资本", "法定代表人"]):
            print("[无工商信息] 页面文本前 300:", text[:300].replace(chr(10), " "), flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
