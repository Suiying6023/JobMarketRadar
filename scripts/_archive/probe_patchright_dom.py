"""读当前 BOSS 搜索页的 DOM，判断登录状态（只看内容，不发业务请求）。

重点：读页面左上角是否显示用户名（登录后）还是「登录/注册」按钮（未登录），
以及 URL 里有没有要求登录的标记。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    print("启动 patchright persistent 浏览器（复用 .browser-profile）...")
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
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(5000)

        print(f"当前 URL: {page.url[:90]}")
        print("\n=== 页面登录相关线索 ===")
        info = await page.evaluate("""() => {
            const text = document.body ? document.body.innerText.slice(0, 3000) : '';
            const out = { hasBody: !!document.body, title: document.title };
            // 找登录相关元素
            const loginBtn = Array.from(document.querySelectorAll('a,span,div'))
                .map(e => e.textContent.trim())
                .find(t => t === '登录' || t === '注册' || t === '扫码登录');
            const nameEl = Array.from(document.querySelectorAll('a,span,div'))
                .map(e => e.textContent.trim())
                .find(t => t.length >= 2 && t.length <= 12 && !/登录|注册|搜索|AI/.test(t)
                    && document.querySelectorAll(`[class*='name']`).length);
            out.loginText = loginBtn || null;
            out.hasUsername = /欢迎|你好/.test(text);
            out.textHead = text.slice(0, 600);
            return out;
        }""")
        print(f"标题: {info['title']}")
        print(f"找到登录按钮文案: {info['loginText']}")
        print(f"有欢迎语: {info['hasUsername']}")
        print(f"\n页面正文前 600 字:\n{info['textHead']}")

        await page.wait_for_timeout(15_000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
