"""找 token 的真实来源（2026-08-10，header 修复后的关键未知量）。

背景：修掉 fetch header bug（headers 被当 init 顶层字段传，三个头全被丢弃）后
发现 `_read_token()` 依赖 `window._PAGE.token`，但 check_account_status 实测
`hasPage: False` —— 这条路径取不到 token。那么修复是否真能带上 token？

本探针用 **被动监听**回答：让前端自己发 wapi 请求，抓它真实带的 header。
不主动发任何 API 请求，只 1 次导航。persistent 模式（非 CDP，避免检测）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

ORIGIN = "https://www.zhipin.com"
PROFILE = ROOT / ".browser-profile"


async def main() -> None:
    from patchright.async_api import async_playwright

    captured: list[dict] = []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def on_request(req):
            if "/wapi/" in req.url:
                h = req.headers
                captured.append({
                    "url": req.url.split("?")[0].replace(ORIGIN, ""),
                    "token": h.get("token", "<无>"),
                    "zp_token": (h.get("zp_token") or "<无>")[:24],
                    "xrw": h.get("x-requested-with", "<无>"),
                    "traceid": (h.get("traceid") or "<无>")[:24],
                })

        page.on("request", on_request)
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(9000)

        print("=== 前端自己发的 wapi 请求真实 header（被动监听）===", flush=True)
        if not captured:
            print("  (未捕获到 wapi 请求)", flush=True)
        for c in captured[:8]:
            print(f"  {c['url']}", flush=True)
            print(f"    token={c['token']} | zp_token={c['zp_token']} | "
                  f"x-requested-with={c['xrw']} | traceid={c['traceid']}", flush=True)

        found = await page.evaluate(
            """() => {
                const out = {};
                out._PAGE_exists = typeof window._PAGE !== 'undefined';
                if (window._PAGE) {
                    out._PAGE_keys = Object.keys(window._PAGE).slice(0, 25);
                    out._PAGE_token = String(window._PAGE.token || '').slice(0, 24);
                }
                const hits = [];
                for (const k of Object.keys(window)) {
                    if (!/token|_PAGE|zp|bst|stoken/i.test(k)) continue;
                    const v = window[k];
                    if (typeof v === 'string' && v) hits.push(k + ' = ' + v.slice(0, 40));
                    else if (v && typeof v === 'object') {
                        try {
                            const sub = Object.keys(v).filter(x => /token/i.test(x));
                            if (sub.length) hits.push(k + ' -> {' + sub.join(',') + '}');
                        } catch (e) {}
                    }
                }
                out.window_hits = hits.slice(0, 15);
                const ls = [];
                try {
                    for (const k of Object.keys(localStorage)) {
                        if (/token|zp|bst/i.test(k)) {
                            ls.push(k + ' = ' + String(localStorage[k]).slice(0, 40));
                        }
                    }
                } catch (e) {}
                out.localStorage_hits = ls.slice(0, 12);
                return out;
            }"""
        )
        print("\n=== window / localStorage ===", flush=True)
        print(json.dumps(found, ensure_ascii=False, indent=2)[:1400], flush=True)

        cookies = await ctx.cookies(ORIGIN)
        print("\n=== 关键 cookie（HttpOnly 也能读到）===", flush=True)
        for c in cookies:
            if any(k in c["name"] for k in ("zp_", "bst", "stoken", "wt2", "sseed", "sname", "sts")):
                print(f"  {c['name']} = {str(c['value'])[:44]}... "
                      f"(httpOnly={c.get('httpOnly')})", flush=True)

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
