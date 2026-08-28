"""探查 BOSS 搜索页筛选面板的真实 DOM 结构（登录态）。

目标：搞清楚筛选条触发器和选项容器的 class/属性，为抓全筛选码做准备。
已知 FILTER_CODES 覆盖经验/学历/薪资/规模/融资/jobType，缺行业/技能/地区/地铁。

用法: python scripts/probe_filter_panel.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

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
        await page.wait_for_timeout(8000)
        print(f"URL: {page.url}\n")

        # 1. 找筛选触发器文本元素，dump 祖先链 class
        trig = await page.evaluate("""() => {
            const labels = ['工作经验','学历要求','薪资待遇','公司行业','公司规模','融资阶段','职位类型','求职类型','技能','工作城市','工作地点','城市'];
            const out = [];
            for (const t of labels) {
                const els = [...document.querySelectorAll('div,span,li,button')]
                    .filter(e => e.childElementCount===0 && e.textContent.trim()===t);
                for (const el of els.slice(0,1)) {
                    const chain = [];
                    let p = el;
                    for (let i=0;i<6 && p;i++) {
                        chain.push((p.tagName||'') + (typeof p.className==='string' ? '.'+p.className.split(' ').filter(Boolean).join('.') : ''));
                        p = p.parentElement;
                    }
                    out.push({text:t, chain:chain.slice(0,5)});
                }
            }
            return out;
        }""")
        print("=== 筛选触发器祖先链 ===")
        for t in trig:
            print(f"  {t['text']}: {' <- '.join(t['chain'][::-1])[:160]}")

        # 2. 点开「公司行业」下拉，dump 面板选项结构
        try:
            t = page.get_by_text("公司行业", exact=True).first
            if await t.count():
                await t.click()
                await page.wait_for_timeout(2000)
                panel = await page.evaluate("""() => {
                    const out = {opts: []};
                    const all = [...document.querySelectorAll('[data-code], [data-val], [data-id], li, [class*="option"]')];
                    const seen = new Set();
                    for (const el of all) {
                        const code = el.getAttribute('data-code') || el.getAttribute('data-val') || el.getAttribute('data-id');
                        const txt = (el.textContent||'').trim().replace(/\\s+/g,' ');
                        if (code && txt && txt.length<15 && !seen.has(code)) {
                            seen.add(code);
                            out.opts.push({code, txt, tag:el.tagName, cls:(el.className||'').toString().slice(0,60)});
                        }
                    }
                    return out;
                }""")
                print(f"\n=== 公司行业面板选项（{len(panel['opts'])} 个） ===")
                for o in panel["opts"][:40]:
                    print(f"  {o['code']}: {o['txt']} [{o['tag']}.{o['cls']}]")
            else:
                print("\n没找到「公司行业」触发器")
        except Exception as exc:
            print(f"行业面板异常: {exc}")

        # 3. 点开「技能」下拉（如果存在）
        try:
            t = page.get_by_text("技能", exact=True).first
            if await t.count():
                await t.click()
                await page.wait_for_timeout(2000)
                panel2 = await page.evaluate("""() => {
                    const opts = [];
                    const all = [...document.querySelectorAll('[data-code], [data-val], [data-id], li, [class*="option"]')];
                    const seen = new Set();
                    for (const el of all) {
                        const code = el.getAttribute('data-code') || el.getAttribute('data-val') || el.getAttribute('data-id');
                        const txt = (el.textContent||'').trim().replace(/\\s+/g,' ');
                        if (code && txt && txt.length<15 && !seen.has(code)) { seen.add(code); opts.push({code, txt}); }
                    }
                    return opts;
                }""")
                print(f"\n=== 技能面板选项（{len(panel2)} 个，前 30） ===")
                for o in panel2[:30]:
                    print(f"  {o['code']}: {o['txt']}")
            else:
                print("\n没找到「技能」触发器")
        except Exception as exc:
            print(f"技能面板异常: {exc}")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
