"""诊断探针：patchright persistent 重启后，token 能否读到？是时序还是登录失效？

expand_list.py 之前启动即报「读不到 token」退出。本探针用同样的启动参数，
把 token 采样拉长到 18 秒 + 打印页面 URL/登录元素，判断：
- 稍等几秒能读到 token → 时序问题，修 expand_list 的等待
- 一直读不到且 URL 是登录页 → 登录态失效，需用户重新登录

用法: python scripts/probe_token_restart.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    from patchright.async_api import async_playwright

    profile = str(Path(cfg.browser.user_data_dir).resolve())
    print(f"profile: {profile}")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded", timeout=30000)
        for i in range(6):
            await page.wait_for_timeout(3000)
            try:
                info = await page.evaluate(
                    """() => {
                        const p = (typeof window._PAGE !== 'undefined') ? window._PAGE : null;
                        return {url: location.href.slice(0, 70),
                                hasPage: !!p,
                                token: p ? (p.token || '') : '',
                                title: document.title.slice(0, 30)};
                    }""")
            except Exception as exc:  # noqa: BLE001
                info = {"url": page.url[:70], "error": str(exc)[:60]}
            print(f"[{i*3}s] {info}", flush=True)
        # 登录元素检查：BOSS 登录页有特定文案/按钮
        try:
            login_mark = await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText.slice(0, 400) : '';
                    return {hasLoginText: t.includes('扫码登录') || t.includes('验证码登录'),
                            isLoginPage: location.href.includes('login')};
                }""")
            print(f"login-check: {login_mark}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"login-check err: {exc}", flush=True)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
