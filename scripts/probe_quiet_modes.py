"""能不能让跑批的 Chrome 不弹窗，又不破坏挑战自解？

**约束（都是实测过的，不要再试）**：
  · `headless=True` —— 无头下挑战不自解（refresh_status_loop 早有注释）
  · `--window-position=-32000,-32000` —— 今天的根因：负坐标被判异常环境，
    请求被推到登录页，0/4。窗口几何属于**指纹面**。

所以候选只剩"不改窗口坐标、也不改可见性标志"的做法：
  A baseline    在屏正常窗口（现状，已知 ok）——作对照
  B minimized   起来后用 Win32 ShowWindow 最小化。窗口几何不变、
                `window.screenX/Y` 仍是正常值，只是不在前台。
  C new_headless `--headless=new` —— Chrome 新版无头，有真实的窗口指纹实现，
                与老 headless 不是一回事，值得实测而不是想当然排除。
  D virtual_desk 放到另一个虚拟桌面：Windows 无公开 API，需要 COM，
                本脚本不测，留作最后手段。

判据只看**能不能取到岗位页**（ok 数），不看"窗口好不好看"。
任何一档只要 ok 掉到 0，就说明它动了指纹，直接淘汰。
"""
from __future__ import annotations

import asyncio
import ctypes
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchright.async_api import async_playwright  # noqa: E402

from job_radar.refresh_fetch import EXTRACT_JS, MIN_JOB_HTML_LEN  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
SW_MINIMIZE = 6


def minimize_chrome() -> int:
    """把所有可见的 Chrome 顶层窗口最小化。返回处理了几个。

    用 ShowWindow(SW_MINIMIZE) 而不是移动坐标：最小化不改 window.screenX/Y
    的报告值（仍是恢复后的位置），所以理论上不碰指纹 —— 这正是要验证的点。
    """
    user32 = ctypes.windll.user32
    count = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        nonlocal count
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0 or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if "Chrome" in buf.value:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            count += 1
        return True

    user32.EnumWindows(_cb, None)
    return count


async def run(mode: str, jobs: list[str], port: int = 1080) -> tuple[int, int, str]:
    args = ["--disable-blink-features=AutomationControlled"]
    kw: dict = {"channel": "chrome", "headless": False,
                "ignore_default_args": ["--enable-automation"]}
    if mode == "new_headless":
        # Chrome 新无头：用 args 传，不用 headless=True（那走老实现）
        args.append("--headless=new")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=args, **kw)
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(f"https://www.zhipin.com/job_detail/{jobs[0]}.html",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"  [{mode}] goto {type(exc).__name__}")
        if mode == "minimized":
            n = minimize_chrome()
            print(f"  [{mode}] 已最小化 {n} 个窗口")
        for _ in range(30):
            if "__zp_stoken__" in {c["name"] for c in await ctx.cookies()}:
                break
            await asyncio.sleep(1.0)
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        # 顺手记下页面自报的窗口几何 —— 若某档在这里就异常，就能解释它为何被拦
        geo = {}
        try:
            geo = await page.evaluate(
                "() => ({sx: window.screenX, sy: window.screenY, "
                "iw: window.innerWidth, ow: window.outerWidth})")
        except Exception:
            pass
        print(f"  [{mode}] title={title[:34]!r} geo={geo}")
        await asyncio.sleep(6)

        ok = ch = 0
        for j in jobs:
            try:
                d = await page.evaluate(
                    EXTRACT_JS,
                    {"url": f"https://www.zhipin.com/job_detail/{j}.html",
                     "minLen": MIN_JOB_HTML_LEN})
            except Exception as exc:
                print(f"  [{mode}] evaluate {type(exc).__name__}")
                ch += 1
                continue
            if d.get("ok"):
                ok += 1
            else:
                ch += 1
            await asyncio.sleep(4)
        await ctx.close()
        await browser.close()
    return ok, ch, title


async def main() -> None:
    con = sqlite3.connect(DB, timeout=30.0)
    jobs = [r[0] for r in con.execute(
        "SELECT source_job_id FROM jobs WHERE retention_status='active' "
        "AND job_description != '' AND source_job_id != '' LIMIT 4")]
    con.close()

    modes = sys.argv[1:] or ["baseline", "minimized", "new_headless"]
    results = []
    for m in modes:
        print(f"=== {m} ===")
        # 每档全新 browser：上次 A/B 共用 browser 导致第二组继承了
        # 第一组暖好的会话，得出假结论。组间必须彻底隔离。
        ok, ch, _ = await run(m, jobs)
        results.append((m, ok, ch))
        print(f"  ⇒ {m}: ok={ok} challenged={ch}\n")
        await asyncio.sleep(8)

    print(f"{'模式':<16}{'ok':>4}{'challenged':>12}  判定")
    for m, ok, ch in results:
        verdict = "可用" if ok > 0 else "**淘汰（动了指纹）**"
        print(f"{m:<16}{ok:>4}{ch:>12}  {verdict}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
