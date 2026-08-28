"""测试推荐路径下详情接口是否免限流（连续取多条）。

背景：单条推荐详情 code=0 JD=274 成功，需验证是否连续多条也免限流。
如果推荐路径详情不触发 code=37，就是绕过详情接口配额的关键通道。

用法: python scripts/probe_recommend_detail.py [--count N] [--delay S]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"
TOKEN = "8GJVqmbrjhi7vZ28"

FETCH = """(payload) => {
    const {url, token} = payload;
    return fetch(url, {credentials: 'include', 'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest', 'token': token})
    .then(async r => ({status: r.status, text: await r.text()}))
    .catch(e => ({status: -1, error: String(e)}));
}"""

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--delay", type=float, default=4.0)
    args = parser.parse_args()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        r = await page.evaluate(FETCH, {"url": f"{ORIGIN}/wapi/zpgeek/recommend/job/list.json",
                                        "token": TOKEN})
        try:
            jobs = ((json.loads(r["text"] or "{}").get("zpData") or {}).get("jobList") or [])
        except json.JSONDecodeError:
            print(f"推荐列表解析失败: {r['text'][:120]}")
            return
        print(f"推荐列表 {len(jobs)} 条\n")

        ok = 0
        for i, j in enumerate(jobs[:args.count], 1):
            sid = j.get("securityId", "")
            lid = j.get("lid", "")
            url = f"{ORIGIN}/wapi/zpgeek/job/detail.json?securityId={sid}&lid={lid}"
            res = await page.evaluate(FETCH, {"url": url, "token": TOKEN})
            text = res.get("text") or ""
            hit = next((t for t in RISK_TEXTS if t in text), None)
            try:
                body = json.loads(text or "{}")
                code = body.get("code")
                zp = body.get("zpData") or {}
                jd = len(str((zp.get("jobInfo") or {}).get("postDescription") or ""))
            except (json.JSONDecodeError, AttributeError):
                code, jd = "?", 0

            if hit:
                print(f"[{i}] RISK「{hit}」: {text[:100]}")
                break
            if code == 0 and jd > 0:
                ok += 1
                print(f"[{i}] OK code=0 JD={jd} {j.get('jobName','')[:22]}")
            else:
                print(f"[{i}] code={code} JD={jd}: {text[:100]}")
                break
            if i < args.count and args.delay > 0:
                await page.wait_for_timeout(args.delay * 1000)

        print(f"\n推荐路径详情: 成功 {ok}/{min(len(jobs), args.count)}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
