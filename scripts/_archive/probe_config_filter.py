"""验证 config 里的筛选组合在真实接口上的效果。

用 patchright 页面内 fetch 请求 joblist.json（带 config 的筛选参数），
检查返回岗位是否都符合：全职 + 本科及以下 + 应届/不限/1年内/1-3年。

用法: python scripts/probe_config_filter.py
"""
from __future__ import annotations

import json
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

# config 里的筛选
ALLOW_EXP = {"应届生", "经验不限", "1年以内", "1-3年"}
ALLOW_DEG = {"初中及以下", "中专/中技", "高中", "大专", "本科"}


async def main() -> None:
    params = {
        "scene": "1", "query": "AI Agent", "city": "101210100", "page": 1, "pageSize": 30,
        "experience": "102,101,103,104", "degree": "209,208,206,202,203", "jobType": "1901",
    }
    url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        result = await page.evaluate(FETCH_TPL, {"url": url, "token": FIXED_TOKEN})
        status = int(result.get("status", 0))
        text = result.get("text") or ""
        hit = next((t for t in RISK_TEXTS if t in text), None)
        if hit:
            print(f"风控「{hit}」: {text[:120]}")
            return
        body = json.loads(text or "{}")
        code = body.get("code")
        jobs = ((body.get("zpData") or {}).get("jobList") or [])
        print(f"HTTP {status} code={code} 返回 {len(jobs)} 条")
        print(f"筛选: 全职 + 本科及以下 + 应届/不限/1年内/1-3年\n")

        bad_exp = bad_deg = 0
        for j in jobs:
            exp = j.get("jobExperience", "")
            deg = j.get("jobDegree", "")
            name = j.get("jobName", "")
            if exp not in ALLOW_EXP:
                bad_exp += 1
                print(f"  ⚠️ 经验越界: {exp} | {name[:24]}")
            if deg not in ALLOW_DEG:
                bad_deg += 1
                print(f"  ⚠️ 学历越界: {deg} | {name[:24]}")
            print(f"  ✓ {exp:6} {deg:6} {name[:26]}")

        print(f"\n越界统计: 经验 {bad_exp} 条, 学历 {bad_deg} 条")
        if bad_exp == 0 and bad_deg == 0:
            print("✅ 配置筛选全部生效")
        else:
            print("⚠️ 有越界，需调整")

        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
