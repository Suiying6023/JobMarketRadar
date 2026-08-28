"""popomv.me：单次请求验证 company_job_list.php 的数据结构 + 找 companyDetail 来源。

登录 → 取一家公司 id → 调一次 company_job_list.php → 打印响应结构。
全程 ~4 个请求，无循环，不给站点加压。
"""
from __future__ import annotations

import asyncio
import json
import re
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
    seen: set[str] = set()

    def on_req(req):
        url = req.url
        if "popomv.me/api" in url and url not in seen:
            seen.add(url)
            print(f"[REQ] {req.method} {url[:140]}", flush=True)

    def on_resp(resp):
        url = resp.url
        if "popomv.me/api" in url and url not in CAPTURE:
            try:
                t = resp.text()
                CAPTURE[url.split("?")[0]] = t
            except Exception:
                pass

    page.on("request", on_req)
    page.on("response", on_resp)
    try:
        # JS 里 companyDetail 来源
        await page.goto(URL, wait_until="networkidle", timeout=45000)
        sources = await page.evaluate(
            """async () => { const o=[]; for (const s of document.scripts)
                if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src))
                    try { o.push(await (await fetch(s.src)).text()); } catch(e){} return o; }"""
        )
        js = "\n".join(sources)
        print("=== companyDetail 赋值来源 ===", flush=True)
        for m in re.finditer(r"companyDetail\s*=\s*[^;]{0,120}", js):
            print("  ...", m.group(0)[:140], flush=True)
        for m in re.finditer(r"(?:getCompanyDetail|fetchCompany|company_detail\w*)\([^)]{0,60}\)", js):
            print("  fn:", m.group(0)[:100], flush=True)

        await page.wait_for_timeout(1500)
        await page.click("text=立即登录", timeout=4000)
        await page.wait_for_timeout(800)
        await page.fill('input[placeholder*="用户名"]', ACCOUNT)
        await page.fill('input[placeholder*="密码"]', PASSWORD)
        await page.evaluate(
            """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
        )
        await page.wait_for_timeout(3000)
        # 取一家公司 id
        lst = CAPTURE.get("/api/v1/company/latest_company_list.php", "")
        data = json.loads(lst or "{}")
        items = data.get("data") or data.get("list") or []
        if isinstance(items, dict):
            items = items.get("list") or items.get("data") or []
        cid = None
        if items:
            cid = items[0].get("id") or items[0].get("companyId") or items[0].get("company_id")
            print(f"\n公司样例: {items[0].get('companyName') or items[0].get('name')} | id={cid} | 字段:{list(items[0].keys())[:15]}", flush=True)
        # 调 company_job_list.php（单次）
        if cid:
            r = await page.evaluate(
                """async (id) => {
                    const r = await fetch(`/api/v1/company/company_detail/basic_info/company_job_list.php?id=${id}`,
                        {method:"POST", headers:{"Content-Type":"application/json"},
                         body: JSON.stringify({isArchitecture:false, pageSize:3, pageIndex:1, jobKeyword:""})});
                    return await r.json();
                }""", cid)
            print("\n=== company_job_list.php 响应 ===", flush=True)
            print(json.dumps(r, ensure_ascii=False)[:1500], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
