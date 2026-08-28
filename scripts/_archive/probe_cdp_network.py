"""用 Playwright 的 CDPSession 被动抓取页面真实请求头（零 JS 注入，页面无感知）。

思路：上一个脚本挂 window.fetch 触发了页面自动刷新——说明不能改前端运行时。
改用浏览器原生 Network 域，纯旁路观察。Playwright 的 new_cdp_session 就能开
Network.enable，不需要额外的 websockets 库。

用法: python scripts/probe_cdp_network.py [秒数]
运行期间请在浏览器里手动搜一次并点开一个岗位，脚本会把 detail.json / joblist.json
的完整请求头打印并落盘，重点找 token / zp_token / traceid 的来源。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "network_probe"
ORIGIN = "https://www.zhipin.com"

WATCH_API = ("/wapi/zpgeek/search/joblist.json", "/wapi/zpgeek/job/detail.json")


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            print("没有 zhipin 页签")
            return
        print(f"监听页面: {page.url[:100]}")

        cdp = await ctx.new_cdp_session(page)

        def on_event(params: dict) -> None:
            url = (params.get("request") or {}).get("url") or ""
            hit = next((w for w in WATCH_API if w in url), None)
            if not hit:
                return
            hdrs = (params.get("request") or {}).get("headers") or {}
            entry = {
                "at": datetime.now().strftime("%H:%M:%S"),
                "api": hit,
                "url": url,
                "headers": hdrs,
            }
            captured.append(entry)
            print(f"\n=== {hit} ===")
            for k, v in sorted(hdrs.items()):
                if k.startswith(":"):
                    continue
                print(f"  {k}: {str(v)[:140]}")

        cdp.on("Network.requestWillBeSent", on_event)
        await cdp.send("Network.enable")

        print(f"\nNetwork 域已开启。请在浏览器里正常操作（搜一次+点一个岗位），监听 {seconds:.0f} 秒。")
        await asyncio.sleep(seconds)

    out = OUT_DIR / f"network-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== 共捕获 {len(captured)} 次业务请求 ===")
    if captured:
        print(f"完整结果: {out}")


if __name__ == "__main__":
    asyncio.run(main())
