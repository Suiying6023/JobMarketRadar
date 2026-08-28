"""探索 BOSS 推荐页的岗位列表接口。

背景：用户想看「最近招聘市场」作为策略参考。BOSS 推荐页 /web/geek/recommend
是按算法推荐岗位的地方，可能有独立的列表接口。本脚本：
1. 打开推荐页，观察它实际请求了哪些接口
2. 抓取推荐列表数据（岗位、公司、薪资）
3. 落盘原始 JSON 供分析

用法: python scripts/probe_recommend.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"
FIXED_TOKEN = "8GJVqmbrjhi7vZ28"
OUT_DIR = ROOT / "data" / "recommend_probe"

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

# 候选推荐接口（参考项目提到的 + 常见路径）
CANDIDATES = [
    "/wapi/zpgeek/pc/recommend/job/list.json",
    "/wapi/zpgeek/recommend/job/list.json",
    "/wapi/zpgeek/pc/recommend/job/recommend/list.json",
    "/wapi/zpgeek/recommend/job/recommend.json",
]

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("启动 patchright 浏览器，先打开推荐页观察请求...")

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

        # 打开推荐页
        await page.goto("https://www.zhipin.com/web/geek/recommend",
                        wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5000)
        print(f"推荐页 URL: {page.url[:90]}")

        # 试探候选接口
        results = {}
        for path in CANDIDATES:
            url = f"{ORIGIN}{path}"
            try:
                result = await page.evaluate(FETCH_TPL, {"url": url, "token": FIXED_TOKEN})
            except Exception as exc:
                print(f"  {path}: evaluate 异常 {exc}")
                continue
            status = int(result.get("status", 0))
            text = result.get("text") or ""
            hit = next((t for t in RISK_TEXTS if t in text), None)
            if hit:
                print(f"  {path}: HTTP {status} 风控「{hit}」")
                results[path] = {"status": status, "risk": hit}
                continue
            try:
                body = json.loads(text or "{}")
                code = body.get("code")
                zp = body.get("zpData") or {}
                keys = list(zp.keys())[:10] if zp else []
                print(f"  {path}: HTTP {status} code={code} zpData 键={keys}")
                results[path] = {"status": status, "code": code, "keys": keys}
                # 如果有 jobList 说明命中，落盘
                if zp.get("jobList"):
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    name = path.strip("/").replace("/", "_") + f"-{stamp}.json"
                    (OUT_DIR / name).write_text(json.dumps(body, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
                    print(f"    ✅ 找到推荐列表，落盘 {name}，{len(zp['jobList'])} 条")
            except json.JSONDecodeError:
                print(f"  {path}: HTTP {status} 非 JSON {text[:80]}")

        print("\n=== 候选接口探测结果 ===")
        for path, res in results.items():
            print(f"  {path}: {res}")

        await page.wait_for_timeout(5000)
        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
