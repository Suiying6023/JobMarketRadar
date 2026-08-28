"""popomv.me：读 JS bundle 源码，枚举全部 API 端点 + 找「招聘」相关接口。

浏览器正常加载 JS，我读取页面已加载的脚本源码 grep 接口路径。零额外服务器压力。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")
ACCOUNT = "Suiying"
PASSWORD = "GXH6023GXH"


async def main() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=False, no_viewport=True,
        args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
        ignore_default_args=["--enable-automation"],
    )
    page = await ctx.new_page()
    try:
        await page.goto(URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(1500)
        # 登录（确保看到全部接口）
        try:
            await page.click("text=立即登录", timeout=4000)
            await page.wait_for_timeout(800)
            await page.fill('input[placeholder*="用户名"]', ACCOUNT)
            await page.fill('input[placeholder*="密码"]', PASSWORD)
            await page.evaluate(
                """() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='登录'); b && b.click(); }"""
            )
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        # 读所有已加载脚本源码
        sources = await page.evaluate(
            """async () => {
                const out = [];
                for (const s of document.scripts) {
                    if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src)) {
                        try {
                            const r = await fetch(s.src);
                            out.push(await r.text());
                        } catch(e) {}
                    }
                }
                return out;
            }"""
        )
        all_js = "\n".join(sources)
        print(f"JS 总字符: {len(all_js)}", flush=True)
        # 枚举 api/v1 端点
        apis = sorted(set(re.findall(r'["\'](/api/v1/[a-zA-Z0-9_/\.\-]+)', all_js)))
        print(f"=== 发现的 API 端点（{len(apis)}）===")
        for a in apis:
            print(f"  {a}", flush=True)
        # 找招聘相关
        print("=== 含「招聘/职位/recruit/job」的片段 ===")
        for kw in ("招聘", "职位", "recruit", "job", "zhaopin", "招聘信息"):
            for m in re.finditer(kw, all_js):
                s = all_js[max(0, m.start()-60):m.start()+60]
                print(f"  [{kw}] ...{s}...", flush=True)
                break  # 每词只打一处
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
