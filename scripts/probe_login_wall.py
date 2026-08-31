"""这两个出口到底是「等人过验证」还是「免登录已不被允许」？

**为什么必须分清**：处置完全相反。
  · verify 态 → 弹窗让人点一下就能救
  · 免登录被拒（推到登录页）→ 弹窗**没有可点的东西**，点了也回登录页，
    反复弹就是纯浪费（用户 2026-08-31 亲眼看到两轮都是登录页）

已知事实（curl 带 -L 实测）：岗位页 302 → `security.html`（可自解那层），
没有 403/code=31 硬封标记。但真实浏览器跑完自解之后**落到登录页**。
所以问题出在自解之后那一跳，`security.html` 本身不是终点。

这个脚本走完整条路径并把**每一次跳转**打出来，看清是谁把我们推去登录。
判据取 URL 序列，不取标题：403 页的标题只有「BOSS直聘」，标题排除法会漏
（refresh_status_loop:213 已经记过这个坑）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchright.async_api import async_playwright  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    con = sqlite3.connect(DB, timeout=30.0)
    row = con.execute(
        "SELECT source_job_id FROM jobs WHERE retention_status='active' "
        "AND job_description != '' AND source_job_id != '' LIMIT 1").fetchone()
    con.close()
    url = f"https://www.zhipin.com/job_detail/{row[0]}.html"

    async with async_playwright() as pw:
        # 与刷新链路同样的启动方式：有头 + chrome channel。
        # 无头下挑战不自解，那样探到的"过不去"是无头造成的假象。
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                        viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()

        hops: list[str] = []
        page.on("framenavigated",
                lambda f: hops.append(f.url) if f is page.main_frame else None)

        print(f"[出口 :{port}] 目标 {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"  goto 异常：{type(exc).__name__}")
        # 给自解 JS 与后续跳转留时间（实测挑战自解需要几秒）
        await page.wait_for_timeout(12_000)

        print("  跳转链：")
        seen = set()
        for h in hops:
            key = h.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            print(f"    → {h[:120]}")

        final = page.url or ""
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        names = {c["name"] for c in (await ctx.cookies())}
        print(f"  终点 URL   {final[:120]}")
        print(f"  终点标题   {title!r}")
        print(f"  stoken     {'有' if '__zp_stoken__' in names else '无'}")

        # 结论按 URL 判，与 refresh_status_loop.probe_job_page 同一套口径
        if "/403.html" in final or "code=31" in final:
            verdict = "blocked（IP 硬封，人工验证无效，只能等解封）"
        elif "/verify.html" in final or "安全验证" in title:
            verdict = "verify（可人工过，弹窗有意义）"
        elif "注册登录" in title or "/passport/zp/login" in final or "/login" in final:
            verdict = ("login（**免登录访问被拒**：弹窗没有可点的东西，"
                       "点了也回登录页，别反复弹）")
        elif "招聘" in title:
            verdict = "ok（岗位页正常，链路可用）"
        else:
            verdict = f"未知（title={title!r}）"
        print(f"  ⇒ 判定：{verdict}")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
