"""popomv.me：找有招聘岗位的公司验证数据结构 + 补全 JobDetailDialog 字段 + companyDetail 来源。"""
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
        await page.goto(URL, wait_until="networkidle", timeout=45000)
        sources = await page.evaluate(
            """async () => { const o=[]; for (const s of document.scripts)
                if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src))
                    try { o.push(await (await fetch(s.src)).text()); } catch(e){} return o; }"""
        )
        js = "\n".join(sources)
        # JobDetailDialog 全部字段：label 字符串
        print("=== JobDetailDialog 字段 label ===", flush=True)
        dlg = re.search(r'name:"JobDetailDialog".{0,4000}', js)
        if dlg:
            for m in re.finditer(r'"label","([^"]{2,12})"', dlg.group(0)):
                print(f"  {m.group(1)}", flush=True)
        # companyDetail 来源：basic_info 相关 fetch
        print("=== basic_info / company_detail fetch ===", flush=True)
        for m in re.finditer(r'fetch\(`?["\']?/api/v1/company/company_detail[^,)\s]{0,120}', js):
            print("  ", m.group(0)[:130], flush=True)
        # companyDetail 是否从 company_search 返回
        for m in re.finditer(r"companyDetail.{0,40}?(?:=|\()(?:json|data|res|e)", js):
            print("  cDet:", m.group(0)[:80], flush=True)

        await page.wait_for_timeout(1000)
        await page.click("text=立即登录", timeout=4000)
        await page.wait_for_timeout(800)
        await page.fill('input[placeholder*="用户名"]', ACCOUNT)
        await page.fill('input[placeholder*="密码"]', PASSWORD)
        await page.evaluate(
            """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
        )
        await page.wait_for_timeout(3000)
        # 搜一家知名招聘公司拿 id
        sr = await page.evaluate(
            """async () => { const r = await fetch('/api/v1/company/company_search.php', {
                method:"POST", headers:{"Content-Type":"application/json"},
                body: JSON.stringify({keyword:"杭州", pageSize:5, pageIndex:1, filters:{}})});
                return await r.json(); }""")
        print("\n=== company_search 结构 ===", flush=True)
        print(json.dumps(sr, ensure_ascii=False)[:600], flush=True)
        items = []
        def walk(o):
            nonlocal items
            if isinstance(o, list) and o and isinstance(o[0], dict):
                items = o; return
            if isinstance(o, dict):
                for v in o.values(): walk(v)
        walk(sr)
        if items:
            first = items[0]
            cid = first.get("id") or first.get("companyId") or first.get("company_id")
            print(f"公司: {first.get('companyName') or first.get('name')} | id={cid}", flush=True)
            print(f"字段: {list(first.keys())}", flush=True)
            if cid:
                jobs = await page.evaluate(
                    """async (id) => { const r = await fetch(`/api/v1/company/company_detail/basic_info/company_job_list.php?id=${id}`,
                        {method:"POST", headers:{"Content-Type":"application/json"},
                         body: JSON.stringify({isArchitecture:false, pageSize:5, pageIndex:1, jobKeyword:""})});
                        return await r.json(); }""", cid)
                print("\n=== company_job_list 响应 ===", flush=True)
                print(json.dumps(jobs, ensure_ascii=False)[:1800], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
