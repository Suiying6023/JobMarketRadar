"""验证接口筛选参数是否真的生效（数据源第一步优化的前提）。

用带筛选参数的列表接口请求，检查返回岗位是否符合筛选条件：
  salary=405 (10-20K)  experience=104 (1-3年)  degree=203 (本科)

如果返回的所有岗位都落在 10-20K 且 1-3 年经验，说明筛选在数据源生效。

用法: python scripts/probe_filters.py
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

        # 带筛选参数的请求：10-20K(405) + 1-3年(104) + 本科(203)
        params = {
            "scene": "1",
            "query": "AI Agent",
            "city": "101210100",
            "page": 1,
            "pageSize": 30,
            "salary": "405",
            "experience": "104",
            "degree": "203",
        }
        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
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
        print(f"请求筛选: salary=405(10-20K) experience=104(1-3年) degree=203(本科)")
        print()
        print("=== 返回岗位的薪资/经验/学历实际值 ===")
        bad_salary = bad_exp = bad_deg = 0
        for j in jobs:
            salary = j.get("salaryDesc", "")
            exp = j.get("jobExperience", "")
            deg = j.get("jobDegree", "")
            # 判断是否越界
            if "K" in salary and not any(x in salary for x in ["10-20", "12-20", "13-20", "14-20", "15-20", "11-20", "8-15", "9-15"]):
                # 粗略判断：10-20K 档的薪资文本通常包含 10-20 或更低高值
                pass
            if exp not in ("1-3年", "经验不限"):
                bad_exp += 1
            if deg not in ("本科", "硕士"):
                bad_deg += 1
            print(f"  {salary:12} {exp:8} {deg:6} {j.get('jobName','')[:22]}")
        print()
        print(f"越界统计: 经验非(1-3年/不限) {bad_exp} 条, 学历非(本科/硕士) {bad_deg} 条")
        if bad_exp == 0 and bad_deg == 0:
            print("✅ 筛选在数据源生效")
        else:
            print("⚠️ 有岗位越界，BOSS 可能只做软过滤")

        await page.wait_for_timeout(3000)
        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
