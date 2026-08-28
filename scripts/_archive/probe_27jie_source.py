"""找 job 详情页「毕业时间」的数据来源：捕获页面加载的 API 请求 + 查 window 数据。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from job_radar.collectors.browser import BrowserSession  # noqa: E402
from job_radar.config import load_config  # noqa: E402

JOB_URL = "https://www.zhipin.com/job_detail/f1a4ba9af69a3edb0nF93tq9GVZV.html"


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    sess = BrowserSession(cfg.browser)
    ctx = await sess.start()
    page = await ctx.new_page()
    seen: set[str] = set()

    def on_req(req):
        url = req.url
        if "zhipin.com" in url and req.resource_type in ("xhr", "fetch") and "/job_detail" not in url:
            if url not in seen:
                seen.add(url)
                print(f"[REQ] {req.method} {url[:160]}", flush=True)

    page.on("request", on_req)
    try:
        await page.goto(JOB_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(6000)
        # 查 window 数据里有没有 毕业时间
        found = await page.evaluate(
            """() => {
                const walk = (o, path, out) => {
                    if (!o || typeof o !== 'object') return;
                    for (const k of Object.keys(o)) {
                        if (typeof o[k] === 'string' && (o[k].includes('毕业时间') || o[k].includes('2027.04'))) {
                            out.push(path + '.' + k + ' = ' + o[k].slice(0, 60));
                        }
                        if (k.includes('grad') || k.includes('deadline') || k.includes('Graduate') || k.includes('campus') || k.includes('recruit') || k.includes('Graduat')) {
                            out.push(path + '.' + k + ' = ' + String(o[k]).slice(0, 40));
                        }
                        if (o[k] && typeof o[k] === 'object' && path.split('.').length < 8) walk(o[k], path + '.' + k, out);
                    }
                };
                const out = [];
                for (const key of Object.keys(window)) {
                    if (key.startsWith('__') || key.startsWith('_') || /[a-z]/.test(key[0])) continue;
                    try { walk(window[key], key, out); } catch(e) {}
                }
                return out.slice(0, 20);
            }"""
        )
        print("=== window 里的毕业时间/校招字段 ===", flush=True)
        for f in found:
            print("  ", f, flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await sess.close()


if __name__ == "__main__":
    asyncio.run(main())
