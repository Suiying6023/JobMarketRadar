"""popomv.me：抽样多家公司，枚举全部招聘来源 srcWebsite + 分析各来源发布时间分布。

目标公司：思伟科技 + 若干随机关键词搜索出的公司。单会话登录，请求数有上限。
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from collections import Counter

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")
ACCOUNT = "Suiying"
PASSWORD = "GXH6023GXH"
TARGETS = ["杭州思伟科技", "软件", "教育", "医药", "传媒", "银行", "智能科技"]


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
    src_counter = Counter()
    src_time: dict[str, list] = {}
    company_jobs: dict[str, int] = {}

    async def search_and_pull(keyword: str, max_pull: int = 2):
        sr = await page.evaluate(
            """async (kw) => { const r = await fetch('/api/v1/company/company_search.php', {
                method:"POST", headers:{"Content-Type":"application/json"},
                body: JSON.stringify({keyword:kw, pageSize:8, pageIndex:1, filters:{}})});
                return await r.json(); }""", keyword)
        items = []
        def walk(o):
            nonlocal items
            if isinstance(o, list) and o and isinstance(o[0], dict):
                items = o; return
            if isinstance(o, dict):
                for v in o.values(): walk(v)
        walk(sr)
        if not items:
            print(f"[{keyword}] 无结果", flush=True)
            return
        pulled = 0
        for it in items:
            if pulled >= max_pull:
                break
            cid = it.get("id")
            name = it.get("companyName")
            if not cid:
                continue
            jobs = await page.evaluate(
                """async (id) => { const r = await fetch(`/api/v1/company/company_detail/basic_info/company_job_list.php?id=${id}`,
                    {method:"POST", headers:{"Content-Type":"application/json"},
                     body: JSON.stringify({isArchitecture:false, pageSize:50, pageIndex:1, jobKeyword:""})});
                    return await r.json(); }""", cid)
            recs = []
            def walk2(o):
                nonlocal recs
                if isinstance(o, list) and o and isinstance(o[0], dict) and "jobName" in o[0]:
                    recs = o; return
                if isinstance(o, dict):
                    for v in o.values(): walk2(v)
            walk2(jobs)
            if recs:
                pulled += 1
                company_jobs[name] = len(recs)
                for j in recs:
                    src = j.get("srcWebsite") or "?"
                    src_counter[src] += 1
                    src_time.setdefault(src, []).append(j.get("publishTime") or "")
                print(f"  [{keyword}] {name}: {len(recs)} 条岗位 | 来源:{','.join(sorted({j.get('srcWebsite') or '?' for j in recs}))}", flush=True)

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
        print("=== 逐家抽样 ===", flush=True)
        for kw in TARGETS:
            await search_and_pull(kw, max_pull=1)
        print("\n=== 来源汇总 ===", flush=True)
        for src, cnt in src_counter.most_common():
            times = sorted(t for t in src_time[src] if t)
            rng = f"{times[0]} ~ {times[-1]}" if times else "无时间"
            print(f"  {src}: {cnt} 条 | 发布时间 {rng}", flush=True)
        print("\n各公司岗位数:", company_jobs, flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
