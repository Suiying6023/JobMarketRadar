"""分析前端真实请求 vs 我们 fetch 的差异：为什么人工浏览不触发 code=37。

假设：前端每次打开职位详情都是独立页面导航，详情页有全新的 _PAGE.token
（= 每次换新 token），而我们同页 fetch 复用同一 token 5-6 次就过期触发。

实测方法：
1. 启动 patchright，导航搜索页
2. 注册网络监听，过滤 detail.json / joblist.json 请求，打印 headers+cookies
3. 真实点击职位卡片（模拟人工），观察前端发的详情请求
4. 连续点开 3-4 个职位，记录每次请求的 token 是否变化、是否触发 code=37

前台运行，浏览器可见。

用法: python scripts/probe_frontend_net.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import ORIGIN, SEARCH_BASE  # noqa: E402
from job_radar.config import load_config  # noqa: E402


def summarize_headers(headers: dict) -> dict:
    """提取关键请求头，省略超长的。"""
    keys = ["token", "referer", "x-requested-with", "x-b3-traceid",
            "x-zhi-telemetry", "cookie", "traceid", "zp-token"]
    out = {}
    for k in keys:
        v = headers.get(k)
        if v is None:
            continue
        if k == "cookie":
            # 只留关键 cookie 名，不泄露完整值
            names = [c.split("=")[0] for c in v.split(";")]
            out["cookie_names"] = [n for n in names if n in (
                "__zp_stoken__", "zp_at", "wt2", "bst", "wb", "__zp_sseed__", "__zp_sts__")]
        else:
            out[k] = (v[:60] + "...") if len(str(v)) > 60 else v
    return out


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
        events = []

        async def on_request(req):
            url = req.url
            if any(p in url for p in ("detail.json", "joblist.json", "job/detail",
                                      "recommend/job", "wapi/zpgeek")):
                events.append(("REQ", url[:120], dict(req.headers)))

        async def on_response(resp):
            url = resp.url
            if any(p in url for p in ("detail.json", "joblist.json", "job/detail",
                                      "recommend/job")):
                try:
                    body = await resp.json()
                except Exception:
                    body = {}
                code = body.get("code") if isinstance(body, dict) else None
                n = len(((body.get("zpData") or {}).get("jobList")) or []) if isinstance(body, dict) else 0
                events.append(("RESP", url[:120], f"code={code} jobs={n}"))

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(
            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={cfg.cities[0].boss_code}",
            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)
        print(f"搜索页: {page.url[:70]}", flush=True)

        # 读取本页 token 基线
        tok = await page.evaluate(
            "() => (typeof window._PAGE !== 'undefined' && window._PAGE) "
            "? (window._PAGE.token || '') : ''")
        print(f"搜索页 _PAGE.token = {tok[:20] or '(空)'}", flush=True)

        # 真实点击职位卡片，模拟人工打开详情
        for i in range(4):
            events.clear()
            print(f"\n--- 点击第 {i+1} 个职位卡片 ---", flush=True)
            try:
                # 用真实鼠标点击（坐标），触发 vue @click 事件
                box = await page.evaluate(
                    """() => {
                        const cards = document.querySelectorAll(
                            '.job-card-wrapper, .job-card-left, [class*="job-card"]');
                        const card = cards[0];
                        if (!card) return null;
                        const r = card.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                    }""")
                if box:
                    await page.mouse.click(box["x"], box["y"])
                    clicked = True
                    print(f"  坐标点击 ({box['x']:.0f},{box['y']:.0f})", flush=True)
                else:
                    clicked = False
                    print(f"  未找到卡片", flush=True)
            except Exception as exc:  # noqa: BLE001
                clicked = False
                print(f"  点击异常: {str(exc)[:60]}", flush=True)
            await page.wait_for_timeout(5000)

            # 当前页面（可能已跳详情页）
            cur_url = page.url
            tok_now = await page.evaluate(
                "() => (typeof window._PAGE !== 'undefined' && window._PAGE) "
                "? (window._PAGE.token || '') : ''")
            print(f"  当前URL: {cur_url[:70]}", flush=True)
            print(f"  当前 _PAGE.token = {tok_now[:20] or '(空)'}", flush=True)
            print(f"  token 变化: {tok != tok_now} (基线 {tok[:12]} → 现在 {tok_now[:12]})",
                  flush=True)

            for ev in events:
                kind, url, info = ev
                if kind == "REQ":
                    print(f"  [REQ] {url}", flush=True)
                    print(f"        headers: {summarize_headers(info)}", flush=True)
                else:
                    print(f"  [RESP] {url} → {info}", flush=True)
            if not events:
                print(f"  (无 detail 请求，可能在列表页没触发跳转)", flush=True)

            # 若跳到了详情页，返回搜索页继续
            if "job_detail" in cur_url or "geek/job" in cur_url and "jobs" not in cur_url:
                await page.go_back()
                await page.wait_for_timeout(3000)
                print(f"  已返回: {page.url[:60]}", flush=True)

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
