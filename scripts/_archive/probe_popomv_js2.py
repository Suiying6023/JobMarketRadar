"""popomv.me：深挖 JS 找招聘数据接口。

从已加载 JS 提取 recruitmentChannel / job-detail 的上下文，找动态接口拼装和招聘 API。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")


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
        sources = await page.evaluate(
            """async () => {
                const out = [];
                for (const s of document.scripts) {
                    if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src)) {
                        try { out.push(await (await fetch(s.src)).text()); } catch(e) {}
                    }
                }
                return out;
            }"""
        )
        all_js = "\n".join(sources)
        print(f"JS 总字符: {len(all_js)}", flush=True)

        # 招聘渠道字段上下文
        for kw in ("recruitmentChannel", "招聘渠道", "job-detail", "recruitment", "招聘信息"):
            print(f"\n=== [{kw}] 上下文 ===", flush=True)
            cnt = 0
            for m in re.finditer(kw, all_js):
                s = all_js[max(0, m.start()-150):m.start()+250]
                s = s.encode().decode("unicode_escape", errors="ignore") if "\\u" in s else s
                print("  ...", s.replace("\n", " ")[:380], flush=True)
                cnt += 1
                if cnt >= 3:
                    break

        # 动态接口拼装：找 api/v1 附近有 + 或 ${ 的
        print("\n=== 动态拼装的接口 ===", flush=True)
        for m in re.finditer(r'["\']((?:api|/api)[^"\']{0,80})["\']\s*\+|\$\{[^}]{0,40}\}["\']?\+', all_js):
            s = all_js[max(0, m.start()-40):m.start()+120]
            print("  ...", s.replace("\n", " ")[:150], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
