"""popomv.me：从 JS 扒招聘来源枚举 + srcWebsite 逻辑 + 原始岗位 URL 模式。零服务器压力。"""
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
            """async () => { const o=[]; for (const s of document.scripts)
                if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src))
                    try { o.push(await (await fetch(s.src)).text()); } catch(e){} return o; }"""
        )
        js = "\n".join(sources)
        print(f"JS 总字符: {len(js)}", flush=True)

        print("\n=== srcWebsite 上下文 ===", flush=True)
        for m in re.finditer(r"srcWebsite", js):
            s = js[max(0, m.start()-60):m.start()+120]
            print("  ...", s.replace("\n", " ")[:170], flush=True)
            if m.start() > 200000:
                break

        print("\n=== 站点来源名（智联/拉勾/猎聘/前程/BOSS/51job 等）===", flush=True)
        seen = set()
        for kw in ("智联招聘", "拉勾", "猎聘", "前程无忧", "boss直聘", "BOSS直聘", "智联", "前程", "51job", "zhaopin", "lagou", "liepin", "jobs.51job", "zhipin"):
            for m in re.finditer(kw, js):
                s = js[max(0, m.start()-50):m.start()+80]
                key = s[:120]
                if key not in seen:
                    seen.add(key)
                    print(f"  [{kw}] ...{s.replace(chr(10),' ')[:150]}...", flush=True)
                break
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
