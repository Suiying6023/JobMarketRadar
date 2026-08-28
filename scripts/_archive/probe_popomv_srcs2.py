"""popomv.me：用正确搜索参数(condition)查思伟科技 + 若干公司，枚举来源与发布时间。"""
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
CONDS = ["杭州思伟科技", "中国平安", "华为", "字节跳动", "用友"]


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
    src_time: dict[str, list] = {}

    async def search_and_pull(cond: str):
        sr = await page.evaluate(
            """async (c) => { const r = await fetch('/api/v1/company/company_search.php', {
                method:"POST", headers:{"Content-Type":"application/json"},
                body: JSON.stringify({condition:c, pageSize:10, pageIndex:1, clickPath:"company-search", filters:{}})});
                return await r.json(); }""", cond)
        items = []
        def walk(o):
            nonlocal items
            if isinstance(o, list) and o and isinstance(o[0], dict):
                items = o; return
            if isinstance(o, dict):
                for v in o.values(): walk(v)
        walk(sr)
        if not items:
            print(f"[{cond}] 无结果", flush=True)
            return
        # 优先挑名字含关键词的
        cand = next((x for x in items if cond.split()[0] in (x.get("companyName") or "")), items[0])
        cid = cand.get("id"); name = cand.get("companyName")
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
        print(f"[{cond}] -> {name}: {len(recs)} 条岗位", flush=True)
        if not recs:
            print("   (无岗位)", flush=True)
        srcs = Counter()
        for j in recs:
            s = j.get("srcWebsite") or "?"
            srcs[s] += 1
            src_time.setdefault(s, []).append(j.get("publishTime") or "")
        for s, c in srcs.most_common():
            ts = sorted(t for t in src_time[s] if t)
            rng = f"{ts[0]}~{ts[-1]}" if ts else "?"
            print(f"   {s}: {c} 条 | 发布时间 {rng}", flush=True)

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
        for c in CONDS:
            await search_and_pull(c)
        print("\n=== 各来源发布时间汇总 ===", flush=True)
        for s, ts in src_time.items():
            ts = sorted(t for t in ts if t)
            print(f"  {s}: {len(ts)} 条 | {ts[0] if ts else '?'} ~ {ts[-1] if ts else '?'}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
