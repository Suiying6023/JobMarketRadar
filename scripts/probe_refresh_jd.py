"""刷新链路能不能顺手把 JD 正文也读回来？

**为什么值得问**：`EXTRACT_JS` 已经在取 `.job-sec-text` 了，只是只留了长度
（`jd_len`）、把正文丢掉。如果那段正文和采集链路（详情接口）拿到的一样完整，
那么刷新一条 = 顺手补一条 JD，**零额外请求**。

对比价值很大：
  · 采集链路补 JD：带账号 cookie + 家宽 IP，日线 900，实测约 1.2 次请求/条
  · 刷新链路：免登录 + 跳板 IP，日线 1000，摊薄约 1.6 次请求/条
两条链路**风控主体完全不同**（不同 IP、不同登录态），日线各算各的。
所以能从刷新侧拿到的 JD，等于凭空多出来的产能——不占采集账号的额度。

**但不能想当然**：详情接口返回的是结构化 JSON（含 skills/活跃度/securityId），
而 HTML 里只有渲染后的文本。要验证的是：
  ① 正文完整吗（会不会被截断、会不会缺"任职要求"那半段）
  ② 和已入库的 JD 一致吗（同一条岗位，两边文本比对）
  ③ 有没有多带垃圾（登录浮层、"更多职位"推荐位——实测整页 innerText 会串味）

判据：拿**库里已有 JD** 的岗位做对照，逐条比长度与内容重合度。
不看"看起来像不像 JD"，看它与权威来源的差异。
"""
from __future__ import annotations

import asyncio
import difflib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from patchright.async_api import async_playwright  # noqa: E402

from job_radar.refresh_fetch import MIN_JOB_HTML_LEN  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"

# 在 EXTRACT_JS 之外单独写一份：这里要**同时**取多种候选选择器，
# 好知道哪个最完整。定型之后再把胜出的那个并回 refresh_fetch。
PROBE_JS = """async ({url, minLen}) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    if (!html || html.length < minLen) {
        return {ok: false, reason: 'challenged', len: (html||'').length};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const txt = (sel) => {
        const el = doc.querySelector(sel);
        return el ? (el.textContent || '').trim() : null;
    };
    return {
        ok: true, len: html.length,
        h1: txt('.info-primary .name h1') || txt('h1'),
        // 候选 1：现用的容器
        sec_text: txt('.job-sec-text'),
        // 候选 2：整个"职位描述"区块（可能含标题行）
        detail_sec: txt('.job-detail-section'),
        // 候选 3：更靠外的容器，看会不会多带推荐位
        job_detail: txt('.job-detail'),
        // 岗位地址与关键词，顺手看还能不能多捞字段
        address: txt('.location-address'),
        tags: Array.from(doc.querySelectorAll('.job-keyword-list li'))
                   .map(e => (e.textContent||'').trim()).filter(Boolean),
    };
}"""


def norm(s: str | None) -> str:
    """归一化再比：HTML 渲染出来的空白与库里存的不同，直接比会全是假差异。"""
    return re.sub(r"\s+", "", s or "")


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    con = sqlite3.connect(DB, timeout=30.0)
    con.row_factory = sqlite3.Row
    # 取**已有 JD** 的岗位做对照：有权威文本才能算重合度。
    rows = con.execute(
        "SELECT source_job_id, title, job_description FROM jobs "
        "WHERE retention_status='active' AND job_description != '' "
        "AND source_job_id != '' AND length(job_description) > 100 "
        "ORDER BY canonical_id LIMIT 6").fetchall()
    con.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        first = rows[0]["source_job_id"]
        try:
            await page.goto(f"https://www.zhipin.com/job_detail/{first}.html",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"[暖场] goto {type(exc).__name__}")
        for _ in range(30):
            if "__zp_stoken__" in {c["name"] for c in await ctx.cookies()}:
                break
            await asyncio.sleep(1.0)
        print(f"[暖场] stoken {'有' if '__zp_stoken__' in {c['name'] for c in await ctx.cookies()} else '无'}")
        await asyncio.sleep(6)

        good = 0
        for r in rows:
            url = f"https://www.zhipin.com/job_detail/{r['source_job_id']}.html"
            try:
                d = await page.evaluate(PROBE_JS,
                                        {"url": url, "minLen": MIN_JOB_HTML_LEN})
            except Exception as exc:
                print(f"\n{r['title'][:26]}: evaluate {type(exc).__name__}")
                continue
            if not d.get("ok"):
                print(f"\n{r['title'][:26]}: {d.get('reason')} len={d.get('len')}")
                await asyncio.sleep(5)
                continue

            db_jd = r["job_description"]
            web = d.get("sec_text") or ""
            a, b = norm(db_jd), norm(web)
            ratio = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0
            print(f"\n{r['title'][:30]}")
            print(f"  库里 JD   {len(db_jd):>6} 字")
            print(f"  .job-sec-text        {len(web):>6} 字  重合度 {ratio*100:.1f}%")
            for key in ("detail_sec", "job_detail"):
                v = d.get(key) or ""
                print(f"  .{key:<18} {len(v):>6} 字")
            print(f"  address={d.get('address')!r} tags={d.get('tags')}")
            # 判据：重合度 >=0.85 视为等价（空白与标点差异不算问题）
            if ratio >= 0.85:
                good += 1
            else:
                # 差在哪一头？截断的话 web 会明显短
                if len(b) < len(a) * 0.8:
                    print("  ⚠️ 网页版明显更短 —— 可能被截断或缺半段")
                elif len(b) > len(a) * 1.2:
                    print("  ⚠️ 网页版明显更长 —— 可能串进了推荐位/浮层")
            await asyncio.sleep(5)

        print(f"\n等价（重合度≥85%）：{good}/{len(rows)}")
        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
