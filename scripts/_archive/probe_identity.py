"""确认当前登录身份，只调用户信息接口，不碰搜索/详情接口。"""
from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

ORIGIN = "https://www.zhipin.com"
USER_API = "/wapi/zpuser/wap/getUserInfo.json"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if ORIGIN in (pg.url or ""):
                page = pg
                break
        if page is None:
            page = await ctx.new_page()
            await page.goto(ORIGIN, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        result = await page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url, {credentials: 'include'});
                    return {status: r.status, text: await r.text()};
                } catch (e) { return {status: -1, text: '', error: String(e)}; }
            }""",
            f"{ORIGIN}{USER_API}",
        )
        body = {}
        try:
            body = json.loads(result.get("text") or "{}")
        except json.JSONDecodeError:
            pass
        zp = body.get("zpData") or {}
        info = {
            "http": result.get("status"),
            "code": body.get("code"),
            "message": body.get("message"),
            "logged_in": bool(zp.get("name") or zp.get("userId")),
            "name": zp.get("name"),
            "phone_tail": (str(zp.get("phone"))[-4:] if zp.get("phone") else None),
            "identity": zp.get("identity"),
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))


asyncio.run(main())
