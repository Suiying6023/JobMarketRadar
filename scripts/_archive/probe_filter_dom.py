"""诊断筛选下拉：点击 current-select 容器后 dump 弹出面板 DOM 结构。

先确认点击是否展开，再找选项元素。用 MutationObserver 捕获点击后新增的节点，
dump 其 class 和文本，定位选项列表的真实选择器。
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
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        # 安装 MutationObserver 捕获点击后新增元素
        await page.evaluate("""() => {
            window.__addedNodes = [];
            const obs = new MutationObserver((muts) => {
                for (const m of muts) {
                    for (const n of m.addedNodes) {
                        if (n.nodeType === 1) {
                            window.__addedNodes.push({
                                tag: n.tagName, cls: n.className || '',
                                text: (n.textContent || '').trim().slice(0, 40),
                            });
                        }
                    }
                }
            });
            obs.observe(document.body, {childList: true, subtree: true});
        }""")

        # 点击「职位类型」的 current-select 容器
        # 用精确文本找占位 span，再点它所在的 current-select 容器
        info_before = await page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('*')).find(
                e => e.childElementCount === 0 && e.textContent.trim() === '职位类型');
            return el ? {tag: el.tagName, cls: el.className, parent: el.parentElement.className} : null;
        }""")
        print("「职位类型」元素:", info_before)

        clicked = await page.evaluate("""() => {
            const el = Array.from(document.querySelectorAll('*')).find(
                e => e.childElementCount === 0 && e.textContent.trim() === '职位类型');
            if (!el) return false;
            const container = el.closest('.current-select') || el.parentElement;
            container.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            return true;
        }""")
        print("dispatch click:", clicked)
        await page.wait_for_timeout(2500)

        added = await page.evaluate("() => (window.__addedNodes || []).slice(0, 60)")
        print(f"\n点击后新增节点 {len(added)} 个:")
        seen = set()
        for n in added:
            key = f"{n['tag']}|{n['cls'][:50]}|{n['text'][:30]}"
            if key in seen:
                continue
            seen.add(key)
            print(f"  {n['tag']} cls={n['cls'][:60]} text={n['text'][:30]}")

        # 也 dump 当前 URL 是否变化
        print("\n当前 URL:", page.url[:100])

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
