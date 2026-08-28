"""验证多选筛选与可选维度（industry / jobType）是否生效。

测试矩阵（每项 1 个请求，列表接口风险低）：
  1. 经验多选-逗号:   experience=101,104
  2. 经验多选-重复:   experience=101&experience=104
  3. 行业筛选:        industry=100701 (互联网?) + 经验不限
  4. jobType 筛选:    jobType=1901 (试常见码)

每个请求后统计返回岗位的实际 experience/industry 分布，
判断筛选是否真的在数据源生效。

用法: python scripts/probe_filters_multi.py
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


async def probe(page, label: str, extra_params: dict) -> None:
    params = {"scene": "1", "query": "AI Agent", "city": "101210100", "page": 1, "pageSize": 30}
    params.update(extra_params)
    # 用列表方式保留重复键
    query_parts = []
    for k, v in extra_params.items():
        if isinstance(v, list):
            for item in v:
                query_parts.append(f"{urlencode({k: item})}")
        else:
            query_parts.append(urlencode({k: v}))
    base = f"{ORIGIN}{API_PATH}?{urlencode({'scene':'1','query':'AI Agent','city':'101210100','page':1,'pageSize':30})}"
    url = base + "&" + "&".join(query_parts)

    result = await page.evaluate(FETCH_TPL, {"url": url, "token": FIXED_TOKEN})
    status = int(result.get("status", 0))
    text = result.get("text") or ""
    hit = next((t for t in RISK_TEXTS if t in text), None)
    if hit:
        print(f"\n【{label}】风控「{hit}」: {text[:100]}")
        return

    body = json.loads(text or "{}")
    code = body.get("code")
    jobs = ((body.get("zpData") or {}).get("jobList") or [])
    print(f"\n【{label}】HTTP {status} code={code} 返回 {len(jobs)} 条")
    print(f"  参数: {extra_params}")

    # 统计经验/行业分布
    exp_dist: dict[str, int] = {}
    ind_dist: dict[str, int] = {}
    for j in jobs:
        e = j.get("jobExperience", "")
        i = j.get("brandIndustry", "") or j.get("industry", "")
        exp_dist[e] = exp_dist.get(e, 0) + 1
        ind_dist[i] = ind_dist.get(i, 0) + 1
    print(f"  经验分布: {exp_dist}")
    print(f"  行业 Top5: {dict(sorted(ind_dist.items(), key=lambda x: -x[1])[:5])}")


async def main() -> None:
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
            wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5000)

        # 1. 经验多选-逗号
        await probe(page, "经验多选-逗号 experience=101,104", {"experience": "101,104"})
        await page.wait_for_timeout(3000)
        # 2. 经验多选-重复参数
        await probe(page, "经验多选-重复 experience=101&experience=104",
                    {"experience": ["101", "104"]})
        await page.wait_for_timeout(3000)
        # 3. 行业筛选
        await probe(page, "行业筛选 industry(互联网)+经验不限",
                    {"experience": "101", "industry": "100701"})
        await page.wait_for_timeout(3000)
        # 4. jobType 筛选
        await probe(page, "jobType 筛选 jobType=1901",
                    {"experience": "101", "jobType": "1901"})

        await page.wait_for_timeout(3000)
        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
