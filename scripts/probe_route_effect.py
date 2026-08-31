"""为什么探针 4/4 ok，而刷新链路同一条 fetch 路径 100% 撞挑战页？

隧道已修好（两个出口都直达岗位页），所以差异只能出在**上下文构造**上。
唯一的区别是刷新链路给 page 装了资源拦截（`p.route("**/*")` 拦
image/media/font + 埋点域），探针没装。

`refresh_status_loop` 的注释里写着「绝不能拦 script/document/xhr：
BOSS 的 JS 挑战靠自家脚本算 __zp_stoken__」——那个约束是对的，
但拦截**本身**还有别的副作用可能被忽略了：
  · Playwright 的 route 拦截会改变请求时序与并发（每个请求多一次往返 IPC）
  · 被 abort 的请求在服务端看是"客户端主动断开"，可能计入行为特征
  · 拦掉埋点域（logapi）= 少交行为数据，反而可能更像机器

这个脚本用**同一个出口、同一批岗位**，只切换"装不装拦截"，跑两组对照。
拦截是唯一变量，所以哪组 ok 就能定位到它。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchright.async_api import async_playwright  # noqa: E402

from job_radar.refresh_fetch import MIN_JOB_HTML_LEN  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

FETCH_JS = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const t = (s) => { const e = doc.querySelector(s); return e ? (e.textContent||'').trim() : null; };
    return {status: r.status, len: html.length,
            h1: t('.info-primary .name h1') || t('h1'),
            jd_len: (t('.job-sec-text') || '').length};
}"""

BLOCKED_TYPES = ("image", "media", "font")
BLOCKED_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
                 "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")


async def run(browser, port: int, jobs: list[str], intercept: bool) -> tuple[int, int]:
    """跑一组，返回 (ok, challenged)。intercept 是唯一变量。"""
    ctx = await browser.new_context(
        user_agent=UA, locale="zh-CN", viewport={"width": 1366, "height": 768},
        proxy={"server": f"socks5://127.0.0.1:{port}"})
    page = await ctx.new_page()
    if intercept:
        async def _route(route):
            req = route.request
            host = (req.url.split("/")[2] if "//" in req.url else "").lower()
            if req.resource_type in BLOCKED_TYPES or any(b in host for b in BLOCKED_HOSTS):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", _route)

    label = "有拦截" if intercept else "无拦截"
    # 暖场：整页导航拿 stoken（与链路一致）
    first = f"https://www.zhipin.com/job_detail/{jobs[0]}.html"
    try:
        await page.goto(first, wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass
    await page.wait_for_timeout(8000)
    names = {c["name"] for c in (await ctx.cookies())}
    title = ""
    try:
        title = await page.title()
    except Exception:
        pass
    print(f"[{label}] stoken={'有' if '__zp_stoken__' in names else '无'}  "
          f"暖场标题={title[:40]!r}")

    ok = ch = 0
    for jid in jobs:
        url = f"https://www.zhipin.com/job_detail/{jid}.html"
        try:
            d = await page.evaluate(FETCH_JS, url)
        except Exception as exc:
            print(f"  [{label}] fetch 异常 {type(exc).__name__}")
            ch += 1
            continue
        good = d["len"] >= MIN_JOB_HTML_LEN and bool(d["h1"]) and d["jd_len"] > 0
        if good:
            ok += 1
        else:
            ch += 1
        print(f"  [{label}] len={d['len']:>7} jd={d['jd_len']:>5} "
              f"{'ok' if good else 'CHALLENGED'}  {str(d['h1'])[:26]}")
        await asyncio.sleep(4)

    await ctx.close()
    return ok, ch


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    con = sqlite3.connect(DB, timeout=30.0)
    jobs = [r[0] for r in con.execute(
        "SELECT source_job_id FROM jobs WHERE retention_status='active' "
        "AND job_description != '' AND source_job_id != '' LIMIT 5")]
    con.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        # 先跑无拦截（探针的配置，已知能成功），再跑有拦截（链路的配置）。
        # 顺序固定：若两组都失败，至少能排除"是不是第二组吃了第一组的额度"。
        a_ok, a_ch = await run(browser, port, jobs, intercept=False)
        print()
        b_ok, b_ch = await run(browser, port, jobs, intercept=True)
        await browser.close()

    print(f"\n{'配置':<10}{'ok':>5}{'challenged':>12}")
    print(f"{'无拦截':<10}{a_ok:>5}{a_ch:>12}")
    print(f"{'有拦截':<10}{b_ok:>5}{b_ch:>12}")
    if a_ok > 0 and b_ok == 0:
        print("\n⇒ 拦截就是原因：链路装的 route 拦截让 fetch 拿不到岗位页。")
    elif a_ok == 0 and b_ok == 0:
        print("\n⇒ 两组都失败：不是拦截的问题，出口/会话仍在被限制。")
    elif a_ok > 0 and b_ok > 0:
        print("\n⇒ 两组都成功：拦截无害，之前的失败另有原因（时序/并发/额度）。")


if __name__ == "__main__":
    asyncio.run(main())
