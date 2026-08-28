"""popomv.me：找招聘信息列表的 API 路径。

从 JS 提取全部 fetch URL + recruitment/job 相关字段，定位招聘数据的接口。
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

        print("=== 全部 fetch URL ===", flush=True)
        fetches = sorted(set(re.findall(r'fetch\("([^"]+)"', all_js)))
        for f in fetches:
            print(f"  {f}", flush=True)

        print("\n=== recruitment/job/岗位 相关字段与上下文 ===", flush=True)
        for kw in ("jobNum", "jobDetail", "jobName", "updateFrequency", "jobList", "recruitment_list", "job_list", "岗位名称", "发布时间", "publishTime", "jobList"):
            for m in re.finditer(kw, all_js):
                s = all_js[max(0, m.start()-100):m.start()+200]
                print(f"  [{kw}] ...{s.replace(chr(10),' ')[:280]}...", flush=True)
                break
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
