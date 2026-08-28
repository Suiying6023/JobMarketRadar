"""探查 v2：点击筛选下拉后 dump 展开面板的真实 HTML，搞清选项结构。

前置：probe_filter_panel.py 已确认触发器 class。现在点击 .current-select 后 dump
展开容器 HTML，找选项元素与 code 属性。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

PROFILE = "D:/Code/Boss/.browser-profile"
ORIGIN = "https://www.zhipin.com"


async def dump_panel(page, sel: str, label: str) -> None:
    try:
        trigger = page.locator(f"{sel} .current-select").first
        if not await trigger.count():
            print(f"[{label}] 无触发器 {sel}")
            return
        await trigger.click()
        await page.wait_for_timeout(2000)
        # dump 点击后展开容器 HTML
        html = await page.evaluate("""(sel) => {
            const root = document.querySelector(sel);
            if (!root) return 'NO ROOT: ' + sel;
            // 找 root 附近的展开面板：root 自身 + 兄弟 + 父容器内所有含 option/panel/popover 的
            const out = [];
            const cands = [root, root.parentElement, root.parentElement && root.parentElement.parentElement];
            for (const c of cands) {
                if (!c) continue;
                const desc = [...c.querySelectorAll('[class]')].filter(e => {
                    const cn = (e.className||'').toString();
                    return /option|panel|popover|list|menu|dropdown|item/i.test(cn) && e.childElementCount < 40;
                }).slice(0, 3);
                for (const d of desc) {
                    out.push(`<${d.tagName} class="${(d.className||'').toString().slice(0,80)}"> ${(d.innerHTML||'').slice(0,500)}`);
                }
            }
            return out.join('\\n---\\n');
        }""", sel)
        print(f"\n=== {label} 展开面板 ===")
        print(html[:2500])
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(600)
    except Exception as exc:
        print(f"[{label}] 异常: {exc}")


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)

        # 公司行业（含二级行业，最复杂）
        await dump_panel(page, ".condition-industry-select", "公司行业")
        # 职位类型
        await dump_panel(page, ".condition-position-select", "职位类型")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
