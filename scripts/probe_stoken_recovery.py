"""stoken 重算后要等多久才真正生效？—— 这是刷新速率的真实瓶颈。

**为什么问这个**：阶梯加速探测（`probe_refresh_speed.py`）在第二档（条间 8-12s）
就停了，停因是「重暖后连续两次仍被挡」。而两个出口 IP 全程 `forbidden-info`
回 `code:0` 干净 —— **所以限制速率的不是 IP 风控，是 stoken 的恢复**。

这个区分很重要，因为它决定加速该往哪个方向做：
  · 若是 IP 风控 → 只能降速或加出口，别无他法
  · 若是 stoken 恢复 → 缩短「重暖后的等待」比缩短「条间间隔」有效得多，
    而且**不增加对平台的压力**（请求总数不变，只是排布更紧）

生产代码现在重暖后固定等 `3-5s`，那个数是我随手取的（第一版立刻重试会被挡，
所以加了个等待），从没量过真实需要多久。

**做法**：反复「打到撞墙 → 重暖 → 等 T 秒 → 试一次」，T 从小到大扫，
记录每个 T 的成功率。T 太小则重试失败（白花一次请求），
T 太大则每个窗口边界都在干等（拖慢吞吐）。

⚠️ 免登录。每轮都要真的把窗口打满才能测，所以本探针必然产生 challenged ——
那是**方法的一部分**，不是故障。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from refresh_status_loop import (  # noqa: E402
    EXITS, REQ_PER_JOB, REQ_PER_RECOVER, exit_status, fetch_job,
    log_requests, used_today, warm_stoken,
)

# 重暖后的等待秒数，从小到大扫。生产当前是 3-5s（随手取的，没量过）。
WAITS = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3, help="每个等待时长试几次")
    ap.add_argument("--gap", type=float, default=6.0, help="窗口内条间间隔")
    ap.add_argument("--daily-cap", type=int, default=1000)
    ap.add_argument("--out", default="data/calibration/stoken-recovery.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_id, title, source_job_id FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT 200").fetchall()

    healthy = []
    for tag, port in EXITS:
        v, note = exit_status(port)
        print(f"[出口] {tag}(:{port}) {v} — {note}")
        if v == "ok":
            healthy.append((tag, port))
    if not healthy:
        print("没有健康出口，退出。")
        return
    print(f"健康出口 {[t for t,_ in healthy]}\n")

    results = {w: {"tries": 0, "ok": 0} for w in WAITS}
    cursor = 0
    stopped = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        tag, port = healthy[0]
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
                                    "shink.zhipin.com", "t.zhipin.com")):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", _route)

        if not await warm_stoken(page, ctx, rows[0]["source_job_id"]):
            print("冷启动失败，退出。")
            await ctx.close(); await browser.close(); return
        log_requests(con, REQ_PER_RECOVER, outcome="warm")

        for wait in WAITS:
            for trial in range(args.trials):
                if used_today(con) > args.daily_cap - 40:
                    stopped = "日线余量不足"
                    break
                # ① 打到撞墙：一直 fetch 直到回挑战页
                hit = False
                for _ in range(9):
                    if cursor >= len(rows):
                        cursor = 0
                    r = rows[cursor]; cursor += 1
                    rec = await fetch_job(page, r["source_job_id"])
                    log_requests(con, REQ_PER_JOB,
                                 outcome="ok" if rec.get("ok") else "challenged")
                    if not rec.get("ok") and rec.get("reason") == "challenged":
                        hit = True
                        break
                    await asyncio.sleep(args.gap)
                if not hit:
                    print(f"  [等待 {wait}s] 9 条内没撞墙，跳过这次试验")
                    continue

                # ② 重暖 → 等 wait 秒 → 试一次
                if not await warm_stoken(page, ctx, r["source_job_id"]):
                    print(f"  [等待 {wait}s] 重暖失败，跳过")
                    continue
                log_requests(con, REQ_PER_RECOVER, outcome="rewarm")
                if wait:
                    await asyncio.sleep(wait)
                if cursor >= len(rows):
                    cursor = 0
                r2 = rows[cursor]; cursor += 1
                rec2 = await fetch_job(page, r2["source_job_id"])
                ok = bool(rec2.get("ok"))
                log_requests(con, REQ_PER_JOB,
                             outcome="ok" if ok else "challenged")
                results[wait]["tries"] += 1
                results[wait]["ok"] += int(ok)
                print(f"  [等待 {wait}s] 第 {trial+1} 次: "
                      f"{'成功' if ok else '仍被挡'}", flush=True)
                await asyncio.sleep(args.gap)
            if stopped:
                print(f"⛔ {stopped}")
                break

        await ctx.close()
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"stopped": stopped,
         "results": {str(k): v for k, v in results.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 56)
    print(f"{'重暖后等待':<12}{'试验':<7}{'成功':<7}{'成功率'}")
    for w in WAITS:
        d = results[w]
        if d["tries"]:
            print(f"{w:<12.0f}{d['tries']:<7}{d['ok']:<7}"
                  f"{d['ok']/d['tries']:.0%}")
    good = [w for w in WAITS
            if results[w]["tries"] >= 2
            and results[w]["ok"] / results[w]["tries"] >= 0.99]
    if good:
        print(f"\n最短的可靠等待: {min(good):.0f}s"
              f"（生产当前是 3-5s 随机）")
    else:
        print("\n没有一个等待时长达到 100% —— 说明重暖后的成功率与等待无关，"
              "\n瓶颈另有其因（可能是窗口本身需要时间恢复，而非 token 生效延迟）。")
    print(f"结果已存 {out}")
    con.close()


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
