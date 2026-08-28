"""popomv.me：页面内直接 fetch 验证 job_list 接口（登录 + 2 个请求）。"""
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
        await page.wait_for_timeout(3000)
        # 直接 fetch 公司列表
        lst = await page.evaluate(
            """async () => { const r = await fetch('/api/v1/company/latest_company_list.php'); return await r.json(); }"""
        )
        print("=== latest_company_list 结构 ===", flush=True)
        print(json.dumps(lst, ensure_ascii=False)[:500], flush=True)
        items = []
        def walk(o):
            nonlocal items
            if isinstance(o, list) and o and isinstance(o[0], dict):
                items = o
                return
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
        walk(lst)
        if items:
            first = items[0]
            cid = first.get("id") or first.get("companyId") or first.get("company_id")
            print(f"\n公司: {first.get('companyName') or first.get('name')} | id={cid}", flush=True)
            if cid:
                jobs = await page.evaluate(
                    """async (id) => { const r = await fetch(`/api/v1/company/company_detail/basic_info/company_job_list.php?id=${id}`,
                        {method:"POST", headers:{"Content-Type":"application/json"},
                         body: JSON.stringify({isArchitecture:false, pageSize:3, pageIndex:1, jobKeyword:""})});
                        return await r.json(); }""", cid)
                print("\n=== company_job_list.php 响应 ===", flush=True)
                print(json.dumps(jobs, ensure_ascii=False)[:1800], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
