"""用**刷新链路自己的代码路径**跑一遍，把 EXTRACT_JS 的原始返回打出来。

前两次诊断都错了（先说隧道修好就恢复、再说拦截无害所以另有原因），
原因都是我在**推断**而不是读原始返回。`probe_route_effect` 用的是自己写的
FETCH_JS，和链路真正用的 `refresh_fetch.EXTRACT_JS` 不是同一段代码 ——
所以它"9/10 ok"证明不了链路能跑。

这次直接 import 链路用的常量与 JS，复现 `new_ctx` 的上下文构造，
然后把每条的 `{ok, reason, status, len}` 原样打出来。
reason 会直接说明是 challenged 还是 fetch_error，不用猜。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from patchright.async_api import async_playwright  # noqa: E402

# 用链路自己的东西，不另写一份 —— 这是这个探针存在的全部意义
from job_radar.refresh_fetch import (  # noqa: E402
    EXTRACT_JS, MIN_JOB_HTML_LEN,
)

DB = ROOT / "data" / "hangzhou_agent.sqlite3"


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    con = sqlite3.connect(DB, timeout=30.0)
    jobs = [r[0] for r in con.execute(
        "SELECT source_job_id FROM jobs WHERE retention_status='active' "
        "AND job_description != '' AND source_job_id != '' LIMIT 6")]
    con.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=False)
        # 复现 new_ctx：proxy 在 context 层（不是 launch 层），viewport 一致，
        # 不设 user_agent（链路也没设）——每一处都照抄，避免"探针和链路不同源"。
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def _route(route):
            req = route.request
            host = (req.url.split("/")[2] if "//" in req.url else "").lower()
            if req.resource_type in ("image", "media", "font") or any(
                b in host for b in ("logapi.zhipin.com", "geetest.com",
                                    "apm-fe.zhipin.com", "hm.baidu.com",
                                    "shink.zhipin.com", "t.zhipin.com")
            ):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", _route)

        # 暖场：与 warm_stoken 同样的做法
        first = f"https://www.zhipin.com/job_detail/{jobs[0]}.html"
        try:
            await page.goto(first, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"[暖场] goto 异常 {type(exc).__name__}")
        for _ in range(30):
            names = {c["name"] for c in (await ctx.cookies())}
            if "__zp_stoken__" in names:
                break
            await asyncio.sleep(1.0)
        names = {c["name"] for c in (await ctx.cookies())}
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        print(f"[暖场] stoken={'有' if '__zp_stoken__' in names else '无'}  "
              f"url={page.url[:90]}")
        print(f"[暖场] title={title[:50]!r}")
        print(f"[暖场] minLen 阈值 = {MIN_JOB_HTML_LEN}\n")

        for i, jid in enumerate(jobs, 1):
            url = f"https://www.zhipin.com/job_detail/{jid}.html"
            try:
                d = await page.evaluate(EXTRACT_JS,
                                        {"url": url, "minLen": MIN_JOB_HTML_LEN})
            except Exception as exc:
                print(f"{i}. evaluate 抛错 {type(exc).__name__}: {str(exc)[:90]}")
                continue
            # 原样打印，不做任何解释 —— 让 reason 自己说话
            print(f"{i}. {d}")
            await asyncio.sleep(5)

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
