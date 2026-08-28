"""popomv.me：看 latest_company_list.php 响应结构，取公司 id，调 job_list 一次。"""
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
CAPTURE = {}


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

    def on_resp(resp):
        url = resp.url
        if "popomv.me/api" in url and url not in CAPTURE:
            try:
                CAPTURE[url.split("?")[0]] = resp.text()
            except Exception:
                pass

    page.on("response", on_resp)
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
        raw = CAPTURE.get("/api/v1/company/latest_company_list.php", "")
        print("=== latest_company_list.php 响应（前 900）===", flush=True)
        print(raw[:900], flush=True)
        try:
            data = json.loads(raw)
        except Exception as e:
            print("解析失败:", e, flush=True)
            data = {}
        # 递归找列表
        def find_list(o):
            if isinstance(o, list) and o and isinstance(o[0], dict):
                return o
            if isinstance(o, dict):
                for v in o.values():
                    r = find_list(v)
                    if r:
                        return r
            return None
        items = find_list(data)
        if items:
            first = items[0]
            print("\n=== 第一家公司 ===", flush=True)
            print(json.dumps(first, ensure_ascii=False)[:600], flush=True)
            cid = first.get("id") or first.get("companyId") or first.get("company_id")
            if cid:
                r = await page.evaluate(
                    """async (id) => { const r = await fetch(`/api/v1/company/company_detail/basic_info/company_job_list.php?id=${id}`,
                        {method:"POST", headers:{"Content-Type":"application/json"},
                         body: JSON.stringify({isArchitecture:false, pageSize:3, pageIndex:1, jobKeyword:""})});
                        return await r.json(); }""", cid)
                print("\n=== company_job_list.php 响应 ===", flush=True)
                print(json.dumps(r, ensure_ascii=False)[:1600], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
