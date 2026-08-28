"""探测 BOSS 公司(gongsi)页的工商信息来源。

打开指定公司 gongsi 页，捕获页面发出的 wapi 请求 + 从渲染后 DOM 提取工商信息。
只读探测，不落库、不采集。用于判断能否通过 BOSS 接口拿到
注册资本/成立日期/法定代表人/企业类型/经营状态 等工商登记数据。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from job_radar.collectors.browser import BrowserSession  # noqa: E402
from job_radar.config import load_config  # noqa: E402

BRAND_ID = "59f3e5a3ba3d0ef20Hd639u0Fg~~"  # 聚能视界
GONGSI = f"https://www.zhipin.com/gongsi/{BRAND_ID}.html"
KW = ["注册资本", "成立日期", "法定代表人", "企业类型", "经营状态", "统一社会信用代码", "注册地址", "实缴资本"]


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    sess = BrowserSession(cfg.browser)
    ctx = await sess.start()
    page = await ctx.new_page()
    seen: set[str] = set()

    def on_request(req) -> None:
        url = req.url
        if any(k in url for k in ("wapi", "brand", "company", "detail")) \
                and "static" not in url and "img.bosszhipin" not in url:
            if url not in seen:
                seen.add(url)
                print(f"[REQ] {url[:180]}", flush=True)

    page.on("request", on_request)
    try:
        print(f"导航: {GONGSI}", flush=True)
        await page.goto(GONGSI, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(7000)  # 等 SPA 渲染 + 工商信息加载
        print("最终URL:", page.url[:120], flush=True)
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        for kw in KW:
            i = text.find(kw)
            if i >= 0:
                print(f"[DOM] {kw}: {text[i:i+70]!r}", flush=True)
        if not any(text.find(k) >= 0 for k in KW):
            print("[DOM] 未找到工商关键词，页面文本前 300 字:", text[:300].replace(chr(10), ' '), flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await sess.close()


if __name__ == "__main__":
    asyncio.run(main())
