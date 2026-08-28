"""抓取当前会话的新鲜 cookie + token，落盘成 pure_http_client.py 可读的格式。

背景：方案 A 需要「cookie 脱离浏览器后仍被接受」。落盘的 manual-20260730 已过期
45 小时（__zp_stoken__ 跨会话会换），必须重抓。本脚本只做抓取，不发任何业务请求。

- cookie：从 CDP context.cookies() 拿全部（含 httpOnly），拼成请求头字符串
- token：读 window._PAGE.token（登录会话内稳定）
- 产出：data/manual_listen/manual-YYYYMMDD-HHMMSS.json（detail.json 条目格式，
  与 pure_http_client.load_cookie_jar 期望一致）

用法: python scripts/fetch_credentials.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "manual_listen"
ORIGIN = "https://www.zhipin.com"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async def run() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
            if page is None:
                print("没有 zhipin 页签，先在浏览器里打开 https://www.zhipin.com/")
                return

            # 读 token（页面可能正在刷新，重试几次）
            token = ""
            for _ in range(5):
                try:
                    token = str(
                        await page.evaluate(
                            """() => (typeof window._PAGE !== 'undefined' && window._PAGE)
                                         ? (window._PAGE.token || '') : ''"""
                        )
                    )
                except Exception:
                    token = ""
                if token:
                    break
            if not token:
                print("!! 读不到 window._PAGE.token，中止")
                return
            print(f"token: {token[:20]}...")

            # 全部 cookie（含 httpOnly）
            cookies = await ctx.cookies()
            jar = {c["name"]: c["value"] for c in cookies}
            print(f"cookie 总数: {len(jar)}")
            for key in ("wt2", "__zp_stoken__", "bst", "__zp_sseed__", "__zp_sts__", "zp_at"):
                v = jar.get(key, "")
                print(f"  {key}: {v[:24]}... len={len(v)}")
            if not jar.get("__zp_stoken__"):
                print("!! 缺 __zp_stoken__，cookie 可能未登录")
                return

            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            stamp = datetime.now(timezone.utc).astimezone()
            name = f"manual-{stamp.strftime('%Y%m%d-%H%M%S')}.json"
            entry = {
                "at": stamp.isoformat(),
                "api": "/wapi/zpgeek/job/detail.json",
                "source": "fetch_credentials.py 主动抓取",
                "request_headers": {
                    "cookie": cookie_str,
                    "token": token,
                },
            }
            path = OUT_DIR / name
            path.write_text(
                json.dumps([entry], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"已落盘: {path}")

    import asyncio

    asyncio.run(run())


if __name__ == "__main__":
    main()
