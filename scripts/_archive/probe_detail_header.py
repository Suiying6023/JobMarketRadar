"""探针：detail.json 请求的 token 头到底需不需要？真实窗口多大？

背景：joblist 无 token 字段，cookie 有 __zp_stoken__/__zp_sseed__/__zp_sts__。
当前 _evaluate_fetch 用 _PAGE.token 但页面为空 → detail 请求无 token 头。
但之前 backfill 能连续抓 5-6 条，说明 cookie 本身撑起了部分额度。

本探针对比两种 detail 请求：
A) 只带 credentials（无 token 头）—— 与当前 backfill 一致
B) 带固定 token 头（8GJVqmbrjhi7vZ28）
分别连续发 detail 请求，测各自窗口大小，找到 token 头是否影响配额。

用法: python scripts/probe_detail_header.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import BossCollector, ORIGIN, API_PATH, DETAIL_PATH  # noqa: E402
from job_radar.config import load_config  # noqa: E402

FIXED_TOKEN = "8GJVqmbrjhi7vZ28"

FETCH_TPL = """(payload) => {
    const {url, token} = payload;
    const headers = {'Accept': 'application/json, text/plain, */*'};
    if (token) headers['token'] = token;
    return fetch(url, {credentials: 'include', ...headers, 'X-Requested-With': 'XMLHttpRequest'})
        .then(r => r.json())
        .catch(e => ({error: String(e)}));
}"""


async def run_series(page, jobs, token, label) -> None:
    """对一批 job 连续发 detail 请求，记录每次 code。"""
    hits = []
    for i, job in enumerate(jobs, start=1):
        security_id = job.get("securityId") or ""
        lid = job.get("lid") or ""
        url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': security_id, 'lid': lid})}"
        body = await page.evaluate(FETCH_TPL, {"url": url, "token": token})
        code = body.get("code") if isinstance(body, dict) else "?"
        hits.append(code)
        mark = " ★" if code == 37 else ""
        print(f"  [{label}] {i:2d} code={code}{mark}", flush=True)
        if code == 37:
            zp = body.get("zpData") or {}
            print(f"       seed={'有' if zp.get('seed') else '无'} name={zp.get('name')} "
                  f"ts={'有' if zp.get('ts') else '无'}", flush=True)
            # 触发后等 2s（模拟恢复窗口观察）
            await page.wait_for_timeout(2000)
    ok = sum(1 for c in hits if c == 0)
    print(f"  [{label}] 共 {len(hits)} 次, code=0 成功 {ok} 次, code=37 {sum(1 for c in hits if c==37)} 次",
          flush=True)


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    from patchright.async_api import async_playwright

    profile = str(Path(cfg.browser.user_data_dir).resolve())
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={cfg.cities[0].boss_code}",
            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        # 抓列表拿 securityId/lid
        collector = BossCollector(cfg)
        collector.context = ctx
        collector.page = page
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else None
        params = collector._params(city, cfg.keywords[0], filters, 1)
        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
        result = await page.evaluate(
            """(payload) => fetch(payload.url, {credentials:'include'})
                .then(r => r.json()).catch(e => ({error: String(e)}))""",
            {"url": url})
        jobs = ((result.get("zpData") or {}).get("jobList") or [])
        print(f"取到 {len(jobs)} 个 job", flush=True)
        if not jobs:
            await ctx.close()
            return

        print("\n=== A) 无 token 头（当前 backfill 行为）===", flush=True)
        await run_series(page, jobs[:8], None, "A")

        print("\n=== B) 带固定 token 头 ===", flush=True)
        await run_series(page, jobs[8:16], FIXED_TOKEN, "B")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
