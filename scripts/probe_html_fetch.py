"""能不能把「整页加载」换成「页面内同源 fetch HTML」——即一条岗位 = 1 次请求。

**这是本轮要验证的核心假设**。采集链路早就在做这件事（`boss.py:_evaluate_fetch`
在已登录页面里同源 fetch JSON 接口，一条详情 = 1 次请求）。刷新链路却在 `page.goto`
整页加载，实测一条 **102 次**请求（拦掉图片仍 39 次）。

关键差别：`fetch()` **不加载子资源**。浏览器只发一个 HTTP 请求把 HTML 拿回来，
不会去下 CSS/JS/图片，也不会执行页面里的埋点与那十几个 wapi 调用。
而 `__zp_stoken__` 是**会话级 cookie**：只要先用真浏览器成功落地一次
（让 JS 挑战自解一次），之后同一 context 里的 fetch 就带着合法 stoken。

**所以要验证三件事**：
  ① 冷启动落地一次后，同源 fetch HTML 能否拿到真正的岗位页（而不是挑战页）
  ② 拿到的 HTML 里**是否含**判定所需的两个字段：关闭状态、招聘者活跃度文案
     （若是纯 SPA 壳、内容全靠 JS 渲染，这条路就走不通）
  ③ 连续 fetch 多条会不会触发风控（stoken 是否够用、是否有配额窗口）

⚠️ 免登录，不带任何账号 cookie。只读公开岗位页。
⚠️ 判据必须**与整页加载的结果逐条对照**，不能只看"fetch 通了"：
   要证明的是「同一条岗位、两种取法、判定结论一致」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import (  # noqa: E402
    CLOSED_MARKERS, _is_closed, parse_active_status,
)

# 冷启动那一条要走整页加载（让 JS 挑战自解、拿到会话级 stoken），
# 顺手把图片/埋点拦掉——它们对拿 stoken 没有贡献。
BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
               "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")

# 页面内同源 fetch。注意 credentials:'include' 是必须的：stoken 在 cookie 里。
# 不发任何自定义头——这是一次普通的文档请求，headers 越像浏览器原生越好。
FETCH_HTML = """async ({url}) => {
    try {
        const r = await fetch(url, {credentials: 'include'});
        const t = await r.text();
        return {status: r.status, url: r.url, len: t.length, text: t};
    } catch (e) {
        return {status: -1, url: '', len: 0, text: '', error: String(e)};
    }
}"""


def strip_html(html: str) -> str:
    """粗暴去标签，只为在纯文本里找判定文案。不追求还原排版。"""
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    h = re.sub(r"(?s)<[^>]+>", "\n", h)
    h = re.sub(r"&nbsp;?", " ", h)
    h = re.sub(r"\n{2,}", "\n", h)
    return h.strip()


def verdicts(text: str) -> dict:
    """从纯文本里取本链路需要的两个判定。与 refresh_status_loop 同源判据。"""
    phrase, days = parse_active_status(text)
    return {"closed": _is_closed(text), "active_phrase": phrase,
            "active_days": days,
            "closed_marker": next((m for m in CLOSED_MARKERS if m in text), None)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6, help="fetch 多少条")
    ap.add_argument("--gap", type=float, default=8.0, help="fetch 间隔秒")
    ap.add_argument("--out", default="data/calibration/html-fetch.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_job_id, title, company_name, recruiter_active_status, "
        "       job_status_desc, job_invalid FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT ?",
        (args.jobs,)).fetchall()
    con.close()

    sent: list[str] = []
    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def handler(route):
            req = route.request
            h = (req.url.split("/")[2] if "//" in req.url else "").lower()
            if req.resource_type in BLOCK_TYPES or any(b in h for b in BLOCK_HOSTS):
                await route.abort()
            else:
                sent.append(req.url)
                await route.continue_()
        await page.route("**/*", handler)

        # ── 阶段一：冷启动，整页加载一条，让挑战自解并拿到会话 stoken ──
        first = rows[0]
        url0 = f"https://www.zhipin.com/job_detail/{first['source_job_id']}.html"
        print(f"[冷启动] 整页加载 {first['title'][:24]}", flush=True)
        try:
            await page.goto(url0, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"  goto 异常（挑战跳转常在超时后完成，继续等）: {type(exc).__name__}")
        loop = asyncio.get_event_loop()
        start = loop.time()
        landed = ""
        while loop.time() - start < 45:
            cur = page.url or ""
            if "/verify.html" in cur:
                landed = "verify"; break
            if "/403.html" in cur or "code=31" in cur:
                landed = "blocked"; break
            try:
                t = await page.title()
            except Exception:
                await asyncio.sleep(1.0); continue
            if t.strip() and "招聘" in t and "注册登录" not in t:
                landed = "ok"; break
            await asyncio.sleep(1.5)
        page_text = ""
        if landed == "ok":
            for _ in range(10):
                try:
                    page_text = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''")
                except Exception:
                    await asyncio.sleep(1.0); continue
                if len(page_text) > 500:
                    break
                await asyncio.sleep(0.8)
        cold_cost = len(sent)
        print(f"  落点={landed} 请求={cold_cost} 正文={len(page_text)}字", flush=True)
        if landed != "ok":
            print("冷启动没落地，后面的 fetch 无从对照。退出。")
            await ctx.close(); await browser.close()
            return

        # 冷启动这条的整页判定，作为 fetch 版的对照基准
        base = verdicts(page_text)
        cookies = {c["name"] for c in await ctx.cookies()}
        print(f"  会话 cookie: {sorted(n for n in cookies if 'stoken' in n.lower() or n in ('bst','__zp_sseed__'))}")
        print(f"  整页判定: {base}\n", flush=True)

        # ── 阶段二：同源 fetch，同一条先自对照，再跑其余各条 ──
        print("[fetch] 同源 fetch HTML（不加载子资源）", flush=True)
        for i, r in enumerate(rows, 1):
            jid = r["source_job_id"]
            url = f"https://www.zhipin.com/job_detail/{jid}.html"
            before = len(sent)
            res = await page.evaluate(FETCH_HTML, {"url": url})
            cost = len(sent) - before
            html = res.get("text") or ""
            text = strip_html(html)
            v = verdicts(text)
            # 挑战页/验证页的特征：正文极短，或标题含请稍候/安全验证
            challenged = ("请稍候" in html[:4000] or "安全验证" in html[:4000]
                          or "security-check" in (res.get("url") or ""))
            rec = {
                "n": i, "job_id": jid, "title": r["title"][:30],
                "status": res.get("status"), "html_len": res.get("len"),
                "text_len": len(text), "requests": cost,
                "challenged": challenged,
                "db_active": r["recruiter_active_status"],
                **v,
            }
            results.append(rec)
            flag = "⚠️挑战页" if challenged else "ok"
            print(f"  [{i}/{len(rows)}] {flag} HTTP {res.get('status')} "
                  f"html={res.get('len')}字节 文本={len(text)}字 请求={cost}次")
            print(f"        关闭={v['closed']}({v['closed_marker']}) "
                  f"活跃度={v['active_phrase']}  库存={r['recruiter_active_status']}"
                  f"  | {r['title'][:22]}", flush=True)
            if i == 1:
                same = (v["closed"] == base["closed"]
                        and v["active_phrase"] == base["active_phrase"])
                print(f"        ↑ 与整页加载对照: {'一致 ✅' if same else '不一致 ❌'}"
                      f"（整页 关闭={base['closed']} 活跃度={base['active_phrase']}）",
                      flush=True)
            if i < len(rows):
                await asyncio.sleep(args.gap)

        await ctx.close()
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"cold_start_requests": cold_cost, "page_verdict": base,
         "fetched": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if not r["challenged"] and r["status"] == 200]
    got_act = [r for r in ok if r["active_phrase"]]
    print("\n" + "=" * 66)
    print(f"冷启动整页: {cold_cost} 次请求")
    print(f"fetch 成功 {len(ok)}/{len(results)} 条，"
          f"每条 {sum(r['requests'] for r in ok)/max(len(ok),1):.1f} 次请求")
    print(f"其中拿到活跃度文案 {len(got_act)}/{len(ok)} 条")
    if ok:
        print(f"\n若可行：一轮 N 条的成本 = {cold_cost} + N×1，"
              f"而不是 N×102")
    print(f"结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
