"""验证：只抓列表接口（joblist.json，不带详情）是否触发风控。

背景：库里 1001 条列表数据都是之前抓的没被封，说明列表接口本身风险低。
本脚本用 patchright persistent 路径翻 5 页列表接口，确认单独跑列表稳定。

用法: python scripts/probe_list_only.py [--pages N] [--delay S]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"
API_PATH = "/wapi/zpgeek/search/joblist.json"
FIXED_TOKEN = "8GJVqmbrjhi7vZ28"

FETCH_TPL = """(payload) => {
    const {url, token} = payload;
    return fetch(url, {
        credentials: 'include',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'token': token,
    }).then(async r => ({status: r.status, text: await r.text()}))
    .catch(e => ({status: -1, text: '', error: String(e)}));
}"""

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--delay", type=float, default=8.0)
    args = parser.parse_args()

    print(f"列表接口探针：{args.pages} 页，间隔 {args.delay}s，token={FIXED_TOKEN}")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(5000)
        print(f"页签: {page.url[:80]}")

        total_jobs = 0
        ok = 0
        for p in range(1, args.pages + 1):
            params = {
                "scene": "1",
                "query": "AI Agent",
                "city": "101210100",
                "page": p,
                "pageSize": 30,
            }
            url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
            try:
                result = await page.evaluate(FETCH_TPL, {"url": url, "token": FIXED_TOKEN})
            except Exception as exc:
                print(f"[页{p}] evaluate 异常: {exc}")
                break
            status = int(result.get("status", 0))
            text = result.get("text") or ""

            hit = next((t for t in RISK_TEXTS if t in text), None)
            if hit:
                print(f"[页{p}] FAIL 风控「{hit}」: {text[:120]}")
                break

            body = json.loads(text or "{}")
            code = body.get("code")
            jobs = ((body.get("zpData") or {}).get("jobList") or [])
            n = len(jobs)
            total_jobs += n
            if status == 200 and code == 0 and n > 0:
                # 抽样一条看字段
                sample = jobs[0]
                print(f"[页{p}] OK HTTP {status} code=0 本页 {n} 条 | 样例: "
                      f"{sample.get('jobName','')[:16]} {sample.get('salaryDesc','')} "
                      f"{sample.get('brandName','')[:12]}")
                ok += 1
            else:
                print(f"[页{p}] FAIL HTTP {status} code={code} 列表 {n} 条: {text[:120]}")
                break

            # 观察页面 URL 稳定
            try:
                u = page.url
            except Exception:
                u = "?"
            if "_security_check" in u or "jobs" not in u:
                print(f"[页{p}] !! 页面 URL 变了: {u[:80]}")

            if p < args.pages and args.delay > 0:
                await page.wait_for_timeout(args.delay * 1000)

        print(f"\n共抓 {total_jobs} 条岗位，成功 {ok}/{args.pages} 页")

        await page.wait_for_timeout(5000)
        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
