"""量 fetch 配额窗口有多大、恢复一次要几次请求，并**双向**验证判定正确性。

`probe_fetch_verdict.py` 的结果：冷启动后 fetch **正好 5 条**成功，第 6 条起
每条都回 41KB 的挑战页。这与采集链路的 `code=37` 形态完全一致
（「每约 5 条一次的 cookie 配额窗口」），只是这里表现为 HTML 挑战页而非 JSON code。

采集链路的解法是 `_recover_code37`：让真实浏览器导航到 `security-check.html`
让页面 JS 重算 `__zp_stoken__`，然后重试。本探针验证同一套解法在匿名 HTML 链路
是否成立，并量三个数：
  ① 窗口大小 —— 一个 stoken 能撑几条 fetch
  ② 恢复代价 —— 重算一次 stoken 要几次 HTTP 请求
  ③ 摊薄后的单条成本 = 1 + 恢复代价/窗口大小

**同时补上 `probe_fetch_verdict.py` 缺的另一半验证**：那轮 5 条全是「已知关闭」，
而一个恒判「关闭」的坏判据在这种单向样本上会 100% 正确。这里必须让
「昨天刚抓的 active」也跑够条数，看它们判「在招」。

⚠️ 免登录。挑战页里带 seed/name/ts，恢复走 security-check 页（比整页岗位页轻）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import ACTIVE_DAYS  # noqa: E402

BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
               "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")

EXTRACT = """async ({url}) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    if (html.length < 60000) {
        return {ok: false, status: r.status, len: html.length,
                head: html.slice(0, 3000)};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const txt = (sel) => {
        const el = doc.querySelector(sel);
        return el ? (el.textContent || '').trim() : null;
    };
    return {
        ok: true, status: r.status, len: html.length,
        h1: txt('.info-primary .name h1') || txt('h1'),
        jobStatus: txt('.job-banner .job-status'),
        activeTime: txt('.boss-active-time'),
        jdLen: (txt('.job-sec-text') || '').length,
    };
}"""


def parse_challenge(head: str) -> dict:
    """从挑战页 HTML 里抠 seed/name/ts。采集链路是从 JSON 的 zpData 拿，
    这里只能从页面内联脚本里正则抠 —— 抠不到就只能整页加载恢复。"""
    out = {}
    for key in ("seed", "name", "ts"):
        m = re.search(rf'''["']?\b{key}["']?\s*[:=]\s*["']([^"']{{1,120}})["']''', head)
        if m:
            out[key] = m.group(1)
    return out


def classify(rec: dict) -> tuple[bool | None, str | None]:
    if not rec.get("ok"):
        return None, None
    closed = bool((rec.get("jobStatus") or "").strip())
    act = (rec.get("activeTime") or "").strip() or None
    if act and act not in ACTIVE_DAYS:
        act = next((p for p in sorted(ACTIVE_DAYS, key=len, reverse=True)
                    if p in act), act)
    return closed, act


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=24, help="总共 fetch 多少条")
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--out", default="data/calibration/fetch-window.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # 交错取样：已知关闭 + 昨天刚抓，让两类都均匀分布在窗口内外，
    # 免得「某一类恰好全落在挑战期」而无法判定。
    closed_rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status, 1 e FROM jobs "
        "WHERE job_status_desc='职位已关闭' ORDER BY detail_fetched_at DESC LIMIT ?",
        (args.jobs // 2,)).fetchall()
    fresh_rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status, 0 e FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc!='职位已关闭' AND job_description!='' "
        "ORDER BY detail_fetched_at DESC LIMIT ?", (args.jobs - args.jobs // 2,)).fetchall()
    con.close()
    rows = []
    for a, b in zip(closed_rows, fresh_rows):
        rows.append(a); rows.append(b)
    print(f"交错样本 {len(rows)} 条（已知关闭 {len(closed_rows)} / 刚抓 {len(fresh_rows)}）\n")

    sent: list[str] = []
    results = []
    recover_costs = []
    window_sizes = []
    since_recover = 0

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

        async def cold_start(job_id: str) -> int:
            """整页加载一条，等 __zp_stoken__ 就位。返回花掉的请求数。"""
            before = len(sent)
            url = f"https://www.zhipin.com/job_detail/{job_id}.html"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            start = loop.time()
            while loop.time() - start < 60:
                names = {c["name"] for c in await ctx.cookies()}
                if "__zp_stoken__" in names:
                    break
                if "/verify.html" in (page.url or ""):
                    break
                await asyncio.sleep(1.0)
            return len(sent) - before

        async def recover(seed: str, name: str, ts: str) -> int:
            """导航 security-check 页重算 stoken。返回花掉的请求数。"""
            before = len(sent)
            old = next((c["value"] for c in await ctx.cookies()
                        if c["name"] == "__zp_stoken__"), "")
            url = (f"https://www.zhipin.com/web/common/security-check.html?"
                   + urlencode({"seed": seed, "name": name, "ts": ts,
                                "callbackUrl": "/"}))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            start = loop.time()
            while loop.time() - start < 40:
                cur = next((c["value"] for c in await ctx.cookies()
                            if c["name"] == "__zp_stoken__"), "")
                if cur and cur != old:
                    break
                await asyncio.sleep(0.6)
            return len(sent) - before

        cold = await cold_start(rows[0]["source_job_id"])
        print(f"[冷启动] {cold} 次请求\n")

        hit = miss = unresolved = 0
        for i, r in enumerate(rows, 1):
            url = f"https://www.zhipin.com/job_detail/{r['source_job_id']}.html"
            before = len(sent)
            rec = await page.evaluate(EXTRACT, {"url": url})
            cost = len(sent) - before
            rcost = 0

            if not rec.get("ok"):
                # 撞挑战页 = 窗口用尽。记下窗口大小，重算 stoken 再重试一次。
                window_sizes.append(since_recover)
                head = rec.get("head") or ""
                ch = parse_challenge(head)
                print(f"  [窗口用尽] 第 {since_recover} 条后撞挑战页"
                      f"（{rec.get('len')}字节），抠到 {list(ch)}", flush=True)
                if {"seed", "name", "ts"} <= set(ch):
                    rcost = await recover(ch["seed"], ch["name"], ch["ts"])
                    print(f"  [恢复] security-check 重算 stoken，{rcost} 次请求",
                          flush=True)
                else:
                    rcost = await cold_start(r["source_job_id"])
                    print(f"  [恢复] 抠不到 seed，退回整页加载，{rcost} 次请求",
                          flush=True)
                since_recover = 0
                before2 = len(sent)
                rec = await page.evaluate(EXTRACT, {"url": url})
                cost += len(sent) - before2

            since_recover += 1
            closed, act = classify(rec)
            expect = bool(r["e"])
            h1 = (rec.get("h1") or "").strip()
            same = bool(h1) and (h1[:8] in r["title"] or r["title"][:8] in h1)
            if closed is None:
                unresolved += 1; mark = "⚠️仍挑战"
            elif closed == expect:
                hit += 1; mark = "✅"
            else:
                miss += 1; mark = "❌"
            results.append({
                "n": i, "job_id": r["source_job_id"], "title": r["title"][:30],
                "expect_closed": expect, "got_closed": closed, "active": act,
                "db_active": r["recruiter_active_status"], "same_job": same,
                "requests": cost, "recover_requests": rcost,
            })
            if rcost:
                recover_costs.append(rcost)
            print(f"[{i}/{len(rows)}] {mark} 期望{'关闭' if expect else '在招'}"
                  f" → {'关闭' if closed else ('在招' if closed is False else '?')}"
                  f"  活跃度={act}（库存 {r['recruiter_active_status']}）"
                  f" 身份符={same} 请求={cost}"
                  f"{f' +恢复{rcost}' if rcost else ''}")
            print(f"        {r['title'][:30]}", flush=True)
            if i < len(rows):
                await asyncio.sleep(args.gap)

        await ctx.close()
        await browser.close()

    p = ROOT / args.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "cold_requests": cold, "window_sizes": window_sizes,
        "recover_costs": recover_costs, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(r["requests"] for r in results)
    n_closed_ok = sum(1 for r in results
                      if r["expect_closed"] and r["got_closed"] is True)
    n_open_ok = sum(1 for r in results
                    if not r["expect_closed"] and r["got_closed"] is False)
    n_open_bad = sum(1 for r in results
                     if not r["expect_closed"] and r["got_closed"] is True)
    print("\n" + "=" * 64)
    print(f"窗口大小实测: {window_sizes}"
          f"（均值 {sum(window_sizes)/len(window_sizes):.1f}）" if window_sizes
          else "窗口未用尽")
    print(f"恢复代价实测: {recover_costs}"
          f"（均值 {sum(recover_costs)/len(recover_costs):.1f} 次）" if recover_costs
          else "未触发恢复")
    print(f"\n判定：已知关闭判对 {n_closed_ok}/{sum(1 for r in results if r['expect_closed'])}"
          f"，刚抓判在招 {n_open_ok}/{sum(1 for r in results if not r['expect_closed'])}"
          f"，刚抓被误判关闭 {n_open_bad}")
    print(f"未解决 {unresolved}，身份不符 "
          f"{sum(1 for r in results if r['got_closed'] is not None and not r['same_job'])}")
    print(f"\n总请求 {cold + total} 次读 {len(results)} 条 = "
          f"{(cold + total)/len(results):.2f} 次/条")
    print(f"对照：整页加载实测 102 次/条；REQ_PER_JOB 当前写死 14")
    print(f"结果已存 {p}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
