"""验证 code=37 的「security-check 重算 stoken → 重试」路径。

调研结论：触发 code=37 后，让真实浏览器访问
  https://www.zhipin.com/web/common/security-check.html?seed=..&name=..&ts=..&callbackUrl=..
页面 JS 约 3 秒生成新 __zp_stoken__，随后重试原请求即可成功。

本脚本：
1. 用 patchright 页面内 fetch 抓详情，直到触发 code=37
2. 捕获 code=37 响应的 seed/name/ts
3. 导航到 security-check.html（带参数）
4. 等 5 秒让页面刷新 stoken
5. 回到搜索页，重试刚失败的那条详情
6. 报告是否成功

用法: python scripts/probe_security_check.py [--count N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

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


def jd_len(text: str) -> tuple:
    """从响应文本提取 (code, jd长度)。"""
    try:
        body = json.loads(text or "{}")
        code = body.get("code")
        zp = body.get("zpData") or {}
        job_info = zp.get("jobInfo") or {}
        jd = len(str(job_info.get("postDescription") or ""))
        return code, jd
    except (json.JSONDecodeError, AttributeError):
        return "?", 0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10)
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

        # 先取推荐列表作为目标
        r = await page.evaluate(FETCH, {"url": f"{ORIGIN}/wapi/zpgeek/recommend/job/list.json",
                                        "token": TOKEN})
        jobs = ((json.loads(r["text"] or "{}").get("zpData") or {}).get("jobList") or [])
        print(f"目标: 推荐列表 {len(jobs)} 条\n")

        ok = 0
        recovered = 0
        for i, j in enumerate(jobs[:args.count], 1):
            sid = j.get("securityId", "")
            lid = j.get("lid", "")
            url = f"{ORIGIN}/wapi/zpgeek/job/detail.json?securityId={sid}&lid={lid}"
            res = await page.evaluate(FETCH, {"url": url, "token": TOKEN})
            text = res.get("text") or ""
            hit = next((t for t in RISK_TEXTS if t in text), None)

            if hit:
                print(f"[{i}] RISK「{hit}」，尝试 security-check 重算...")
                try:
                    body = json.loads(text or "{}")
                    zp = body.get("zpData") or {}
                    seed = zp.get("seed", "")
                    name = zp.get("name", "")
                    ts = zp.get("ts", "")
                except json.JSONDecodeError:
                    seed = name = ts = ""
                if not (seed and name and ts):
                    print(f"   缺少 seed/name/ts，无法重算: {text[:120]}")
                    break

                # 导航到 security-check.html
                sec_url = (f"{ORIGIN}/web/common/security-check.html?"
                           f"{urlencode({'seed': seed, 'name': name, 'ts': ts,
                                         'callbackUrl': '/web/geek/jobs'})}")
                print(f"   导航 security-check: {sec_url[:90]}...")
                await page.goto(sec_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(6000)  # 等页面 JS 生成新 stoken
                print(f"   security-check 页面 URL: {page.url[:80]}")

                # 回搜索页
                await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)

                # 重试
                res2 = await page.evaluate(FETCH, {"url": url, "token": TOKEN})
                code2, jd2 = jd_len(res2.get("text") or "")
                if code2 == 0 and jd2 > 0:
                    print(f"   ✅ 重算后成功 JD={jd2}")
                    ok += 1
                    recovered += 1
                else:
                    print(f"   重算后仍失败 code={code2}: {(res2.get('text') or '')[:100]}")
                    break
            else:
                code, jd = jd_len(text)
                if code == 0 and jd > 0:
                    print(f"[{i}] OK JD={jd} {j.get('jobName','')[:22]}")
                    ok += 1
                else:
                    print(f"[{i}] code={code} JD={jd}: {text[:80]}")
                    break
            if i < args.count:
                await page.wait_for_timeout(3000)

        print(f"\n结果: 成功 {ok} 条，其中 security-check 重算救回 {recovered} 条")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
