"""判定「100% 撞挑战页」是真撞墙，还是判据阈值过期了。

**为什么要这一步**：刷新链路 04:00 起 `ok` 归零、全部记成 challenged，而
出口 `forbidden-info` 全是 `code:0`（无风控）。两种解释都能产生这个现象：
  ① 真撞墙 —— stoken 不被接受，服务端返回 41KB 挑战页
  ② **判据过期** —— 平台改版后正常页变小，而我们用 `len < 60000` 判挑战页，
     于是把成功的响应误判成挑战页（假阴性）

线索：这次探测看到 security JS 的 `name=f34f16c6`，而记忆里记的是 `864328f8`
—— 平台侧有过部署。改版后页面体积变化是很常见的伴随结果。

两者的处置完全相反：① 要静置等恢复；② 要改阈值，静置一万年也没用。
所以必须先分清，不能凭"挑战率 100%"就下结论。

判据不看长度，**看内容**：正常岗位页含 `.info-primary .name h1`（岗位标题）
与 `.job-sec-text`（JD 正文）；挑战页含 security JS 的引用且没有这些容器。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchright.async_api import async_playwright  # noqa: E402

from job_radar.refresh_fetch import EXTRACT_JS, MIN_JOB_HTML_LEN  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 只取长度与关键容器，**不落库、不改任何状态**——这是观测脚本。
PROBE_JS = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const t = (s) => { const e = doc.querySelector(s); return e ? (e.textContent||'').trim() : null; };
    return {
        status: r.status,
        len: html.length,
        h1: t('.info-primary .name h1') || t('h1'),
        job_status: t('.job-banner .job-status'),
        active: t('.boss-active-time'),
        jd_len: (t('.job-sec-text') || '').length,
        has_security_js: html.includes('security-js') || html.includes('/security.html'),
        title: (doc.querySelector('title') || {}).textContent || '',
    };
}"""


async def main() -> None:
    con = sqlite3.connect(DB, timeout=30.0)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_job_id, title, company_name FROM jobs "
        "WHERE retention_status='active' AND job_description != '' "
        "AND source_job_id != '' LIMIT 4").fetchall()
    con.close()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080

    async with async_playwright() as pw:
        # channel="chrome" + headless=False：与刷新链路完全一致。
        # ⚠️ 不能改成无头——挑战页在无头下不自解（refresh_status_loop:636 的注释），
        # 那样探到的"撞墙"是无头造成的，不是平台状态，等于测了个假象。
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN")
        page = await ctx.new_page()

        first = rows[0]["source_job_id"]
        url = f"https://www.zhipin.com/job_detail/{first}.html"
        print(f"[暖场] 整页导航 {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6000)
        names = {c["name"] for c in (await ctx.cookies())}
        print(f"[暖场] __zp_stoken__ {'有' if '__zp_stoken__' in names else '**无**'}")
        print(f"[暖场] 页面标题 {(await page.title())!r}")
        print(f"[暖场] 落地 URL {page.url[:110]}")
        # 整页导航拿到的 HTML 长度 —— 这是"正常页多大"的权威参照，
        # 因为它走的是浏览器正常渠道（会执行挑战 JS 并自动跳回）。
        full = await page.evaluate("() => document.documentElement.outerHTML.length")
        print(f"[暖场] 整页 DOM 长度 {full}")

        print(f"\n{'岗位':<26}{'status':>7}{'len':>8}{'jd':>6}  h1 / 判读")
        print("-" * 96)
        for r in rows:
            u = f"https://www.zhipin.com/job_detail/{r['source_job_id']}.html"
            try:
                d = await page.evaluate(PROBE_JS, u)
            except Exception as exc:
                print(f"{r['title'][:24]:<26}  fetch 异常 {type(exc).__name__}: {exc}")
                continue
            # 现行判据 vs 内容判据，两者并排打出来才能看出分歧
            by_len = "challenged" if d["len"] < MIN_JOB_HTML_LEN else "ok"
            by_content = "ok" if (d["h1"] and d["jd_len"] > 0) else "challenged"
            flag = "  ⚠️ 判据分歧" if by_len != by_content else ""
            print(f"{r['title'][:24]:<26}{d['status']:>7}{d['len']:>8}"
                  f"{d['jd_len']:>6}  {str(d['h1'])[:28]}")
            print(f"{'':<26}按长度={by_len}  按内容={by_content}"
                  f"  securityJS={d['has_security_js']}  title={d['title'][:34]!r}{flag}")
            await asyncio.sleep(3)

        print(f"\n现行阈值 MIN_JOB_HTML_LEN = {MIN_JOB_HTML_LEN}")
        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
