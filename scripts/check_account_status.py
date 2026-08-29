"""前台检查账号状态：打开搜索页，读登录态/封禁/验证页。

用法: python scripts/check_account_status.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"
# 窗口保持时长。判断"硬封 vs 可人工过验证"要点开几个入口看，30 秒不够。
HOLD_SECONDS = 600


async def main() -> None:
    print("前台打开浏览器检查账号状态...", flush=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        # 等页面稳定（_security_check 跳转 / 封禁页跳转）
        for _ in range(10):
            await page.wait_for_timeout(1500)
            try:
                if "jobs" in page.url or "403" in page.url or "passport" in page.url or "verify" in page.url:
                    break
            except Exception:
                pass

        url = page.url
        print(f"\n当前 URL: {url[:100]}")

        # 判定页面类型
        status = "正常搜索页"
        if "403" in url:
            status = "🔴 封禁页(403)"
        elif "passport" in url:
            status = "🔴 登录/认证页"
        elif "verify" in url or "security" in url:
            status = "🟡 验证页"
        print(f"页面状态: {status}")

        # 读登录态
        try:
            info = await page.evaluate("""() => {
                const p = window._PAGE || {};
                return {
                    hasPage: typeof window._PAGE !== 'undefined',
                    token: (p.token || '').slice(0, 16),
                    userId: (p.userId || '').toString().slice(0, 12),
                    name: p.name || '',
                };
            }""")
            print(f"登录态: {info}")
        except Exception as exc:
            print(f"读登录态失败: {exc}")

        # 读页面正文开头（看是否封禁文案）
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 300) : ''")
            print(f"\n页面正文前 300 字:\n{text}")
        except Exception as exc:
            print(f"读正文失败: {exc}")

        # 问平台自己的状态接口 —— 这是**权威判定**，比看页面文案可靠。
        # 带登录态发，才能拿到账号维度的结论（匿名请求只回出口 IP）。
        # 2026-08-29 采集吃到 code=36 后就是靠它区分「硬封」与「可人工救」。
        try:
            info = await page.evaluate("""async () => {
                const r = await fetch('/wapi/zpsecureflow/forbidden-info?',
                    {credentials: 'include',
                     headers: {'Accept': 'application/json, text/plain, */*',
                               'X-Requested-With': 'XMLHttpRequest'}});
                return {status: r.status, text: (await r.text()).slice(0, 600)};
            }""")
            print(f"\nforbidden-info（权威判定）: {info}")
        except Exception as exc:
            print(f"查 forbidden-info 失败: {exc}")

        # ⚠️ 刻意**不**在这里探 detail.json：那正是 2026-08-29 吃到 code=36 的端点，
        # 在账号刚被标记时再拿账号 cookie 去打它，是往已触发的风控上加压。
        # forbidden-info 已经给出权威判定，不需要用贵端点复核。
        # 要确认采集是否恢复，等状态正常后跑一次小批量采集即可。

        # 保持窗口打开供人工观察。默认 10 分钟够看完页面各处（原先 30 秒，
        # 判断"是硬封还是可人工过验证"需要点开几个入口，30 秒不够）。
        # 看完直接关浏览器窗口即可，脚本会随之退出。
        print(f"\n浏览器保持打开 {HOLD_SECONDS // 60} 分钟供观察。")
        print("看完直接关窗口即可（也可 Ctrl+C 结束脚本）。")
        try:
            await page.wait_for_timeout(HOLD_SECONDS * 1000)
        except Exception:
            # 人工关掉窗口会让 page/context 失效并抛错 —— 那是正常的结束方式，
            # 不该打成一条看起来像故障的 traceback。
            print("窗口已关闭。")
        try:
            await ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
