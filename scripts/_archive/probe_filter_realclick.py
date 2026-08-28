"""用真实 click + 请求捕获，找筛选下拉展开时前端拉取的配置接口。

patchright 的 .click() 是真实浏览器事件（isTrusted=true），Vue 会响应。
同时挂 page.on('request') 捕获点击下拉后前端发起的请求——如果前端去拉
行业/职位配置接口，就能拿到真实码。

用法: python scripts/probe_filter_realclick.py
"""
from __future__ import annotations

import asyncio
import json

from patchright.async_api import async_playwright

PROFILE = "D:/Code/Boss/.browser-profile"
ORIGIN = "https://www.zhipin.com"


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # 捕获所有请求
        requests = []
        def on_request(req):
            url = req.url
            if "zhipin.com" in url and not any(x in url for x in ["socket", "apm", "js/", "css/", ".png", ".jpg", ".gif", ".woff"]):
                requests.append(url)
        page.on("request", on_request)

        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)
        requests.clear()  # 清掉初始加载的

        # 真实点击「职位类型」下拉（点 current-select 容器，不是 span）
        clicked = page.locator(".current-select", has_text="职位类型").first
        if await clicked.count():
            await clicked.click()
            await page.wait_for_timeout(2000)
            print("已点击职位类型下拉")
        else:
            print("没找到职位类型下拉")

        # 读点击后发起的请求
        print(f"\n点击后捕获 {len(requests)} 个请求:")
        for u in requests:
            print(f"  {u[:120]}")

        # 读当前 URL（筛选是否更新了 query）
        print(f"\n当前 URL: {page.url[:130]}")

        # dump 下拉展开后的面板结构
        panel = await page.evaluate("""() => {
            const out = {visible: []};
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const cs = getComputedStyle(el);
                if (cs.display !== 'none' && cs.visibility !== 'hidden' && el.offsetParent !== null) {
                    const code = el.getAttribute('data-code') || el.getAttribute('code') || el.getAttribute('data-val');
                    if (code) out.visible.push({code, text: el.textContent.trim().slice(0,20), tag: el.tagName});
                }
            }
            return out;
        }""")
        print(f"\n可见的 data-code 元素 {len(panel['visible'])} 个:")
        seen = set()
        for it in panel["visible"]:
            k = it["code"]
            if k in seen:
                continue
            seen.add(k)
            print(f"  {it['code']}: {it['text']}")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
