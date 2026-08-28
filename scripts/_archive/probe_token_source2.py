"""探针：BOSS 搜索页 joblist.json 响应结构 + 前端 token 真实来源。

背景：之前探针发现搜索页 _PAGE.token 为空，但前端能正常请求 joblist 且不触发
code=37。需要搞清：
1. 前端 detail/joblist 请求带的有效 token 到底从哪来
2. joblist 响应 zpData 里是否有 token
3. 页面 window 全局里 token 存在哪

用法: python scripts/probe_token_source2.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import BossCollector, ORIGIN, API_PATH  # noqa: E402
from job_radar.config import load_config  # noqa: E402


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
        print(f"url: {page.url[:70]}", flush=True)

        # 1) window 上所有含 token 的全局
        print("\n=== window token 全局 ===", flush=True)
        tokens = await page.evaluate(
            """() => {
                const out = [];
                try {
                    if (window._PAGE) out.push({k: '_PAGE', v: JSON.stringify(window._PAGE).slice(0, 200)});
                } catch(e) {}
                for (const k of Object.keys(window)) {
                    if (k.toLowerCase().includes('token') || k.toLowerCase().includes('stoken')) {
                        let v;
                        try { v = String(window[k]).slice(0, 80); } catch(e) { v = '?'; }
                        out.push({k, v});
                    }
                }
                return out;
            }""")
        for t in tokens:
            print(f"  {t['k']} = {t['v']}", flush=True)

        # 2) 抓 joblist 响应，看 zpData 结构
        print("\n=== joblist.json 响应 ===", flush=True)
        collector = BossCollector(cfg)
        collector.context = ctx
        collector.page = page
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else None
        params = collector._params(city, cfg.keywords[0], filters, 1)
        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
        result = await page.evaluate(
            """(payload) => fetch(payload.url, {credentials:'include'})
                .then(r => r.json())
                .catch(e => ({error: String(e)}))""",
            {"url": url})
        if "error" in result:
            print(f"  fetch error: {result['error']}", flush=True)
        else:
            print(f"  code={result.get('code')} msg={str(result.get('message'))[:40]}", flush=True)
            zp = result.get("zpData") or {}
            print(f"  zpData keys: {list(zp.keys())}", flush=True)
            for k in zp:
                if "token" in k.lower():
                    print(f"  zpData.{k} = {str(zp[k])[:80]}", flush=True)
            jl = zp.get("jobList") or []
            if jl:
                print(f"  jobList[0] keys({len(jl[0])}): {list(jl[0].keys())[:20]}", flush=True)
                for k in ("securityId", "encryptJobId"):
                    if k in jl[0]:
                        print(f"  job[0].{k} = {str(jl[0][k])[:50]}", flush=True)

        # 3) cookie 存在性
        print("\n=== cookies ===", flush=True)
        names = [c["name"] for c in await ctx.cookies()]
        for n in ("__zp_stoken__", "bst", "zp_at", "wt2", "wb", "__zp_sseed__", "__zp_sts__"):
            print(f"  {n}: {'✓' if n in names else '✗'}", flush=True)

        # 4) DOM 卡片选择器
        print("\n=== DOM 卡片 ===", flush=True)
        dom = await page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('[class*="job-card"]')) {
                    out.push(String(el.className).slice(0, 60));
                    if (out.length >= 8) break;
                }
                return out;
            }""")
        for d in dom:
            print(f"  class: {d}", flush=True)
        print(f"  job-card 数: {len(dom)}", flush=True)

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
