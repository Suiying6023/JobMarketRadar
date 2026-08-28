"""抓取 BOSS 搜索页筛选下拉的真实筛选码（职位类型 / 公司行业等）。

通过点击下拉控件 → 读弹出面板里的选项文本和 data-code 属性。
BOSS 的筛选下拉是 `current-select` 容器，点开后有选项列表。

用法: python scripts/grab_filter_codes.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

PROFILE = "D:/Code/Boss/.browser-profile"
ORIGIN = "https://www.zhipin.com"
OUT = Path("D:/Code/Boss/data/filter_codes.json")


async def grab_dropdown(page, trigger_text: str, out_key: str, results: dict) -> None:
    """点开文本为 trigger_text 的下拉，读取选项。"""
    try:
        # 找到当前下拉触发器
        trigger = page.get_by_text(trigger_text, exact=True).first
        if not await trigger.count():
            print(f"  [{trigger_text}] 没找到触发器")
            return
        await trigger.click()
        await page.wait_for_timeout(1500)

        # 读弹出面板里的选项（通常带 data-code 或点后有 URL 变化）
        info = await page.evaluate("""() => {
            const out = {items: [], url: location.href};
            // 常见选项容器：ul li / .option-list .option-item
            const candidates = document.querySelectorAll('[class*="option"], [class*="list"] li, .dropdown-item, [data-code]');
            const seen = new Set();
            for (const el of candidates) {
                const code = el.getAttribute('data-code') || el.getAttribute('code') || el.getAttribute('data-val');
                const text = el.textContent.trim();
                if (text && text.length < 20 && code && !seen.has(code)) {
                    seen.add(code);
                    out.items.push({code, text});
                }
            }
            return out;
        }""")
        results[out_key] = info
        n = len(info.get("items", []))
        print(f"  [{trigger_text}] 抓到 {n} 个选项:")
        for it in info.get("items", [])[:20]:
            print(f"    {it['code']}: {it['text']}")

        # 关闭下拉（按 Esc）
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    except Exception as exc:
        print(f"  [{trigger_text}] 异常: {exc}")


async def main() -> None:
    results: dict = {}
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city=101210100",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        # 依次抓下拉
        for label, key in [("职位类型", "position"), ("公司行业", "industry"),
                           ("求职类型", "job_type"), ("薪资待遇", "salary"),
                           ("工作经验", "experience"), ("学历要求", "degree")]:
            await grab_dropdown(page, label, key, results)
            await page.wait_for_timeout(1000)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
