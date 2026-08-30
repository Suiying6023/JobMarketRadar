"""阶梯加速探测刷新链路的速率上限，**带自动停止判据**。

目的：找到「条间间隔压到多小仍然安全」的边界。当前默认 12-18s 是按
「一条 1.7-2 次请求 → 约 6 请求/分钟」倒推的，对标 08-11 那轮实测安全的均值 6.7。
但那个 6.7 来自**另一条链路**（带账号 cookie、家宽 IP），拿它约束匿名机房 IP
只是缺乏数据时的保守借用 —— 真实上限未知。

**做法**：条间间隔逐档下调，每档跑固定条数，每档结束后体检两个出口。
出现任一停止信号就立刻收手，并把结论落盘。

**停止判据（任一命中即停，不再往下加速）**：
  ① 出口 `forbidden-info` 回 code=31（IP 被标记）—— 最硬的信号，直接停
  ② 该档 challenged 率 > 40% —— 窗口在塌，再快只是空转烧请求
  ③ 该档出现「重暖后连续两次仍被挡」≥2 次 —— stoken 已经跟不上
  ④ 日线余量不足下一档所需

⚠️ 这是**主动逼近边界**的探测，会真的把 IP 推向被标记。跑之前确认：
  · 两个出口当前都 ok（脚本会自己先查）
  · 本链路免登录，被标记的代价是等约 24 小时或人工过验证，**不影响采集账号**
  · 采集链路的账本不受影响（kind 不同，日线各算各的）

⚠️ 全程免登录。绝不在任何窗口里登录账号。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar.refresh_fetch import (  # noqa: E402
    FETCH_WINDOW, is_closed_status, same_job,
)
from check_job_status import parse_active_status  # noqa: E402
from refresh_status_loop import (  # noqa: E402
    EXITS, MIN_WINDOW, REQ_PER_JOB, REQ_PER_RECOVER, exit_status, fetch_job,
    log_requests, used_today, warm_stoken,
)

# 阶梯：(条间下限, 条间上限)。从当前默认往下压。
LADDER = [(12.0, 18.0), (8.0, 12.0), (5.0, 8.0), (3.0, 5.0), (2.0, 3.0), (1.0, 2.0)]

CHALLENGE_RATE_STOP = 0.40   # 该档 challenged/取值 超过此值即停
DOUBLE_FAIL_STOP = 2         # 「重暖后连试两次仍被挡」出现这么多次即停


async def run_step(page, ctx, con, rows, gap, state) -> dict:
    """跑一档。返回该档统计。state 跨档保留 stoken 窗口状态。"""
    stats = Counter()
    double_fail = 0
    t0 = datetime.now()

    for r in rows:
        # 主动重暖（与生产同逻辑，下限 MIN_WINDOW）
        if state["since_warm"] >= state["window"]:
            if await warm_stoken(page, ctx, r["source_job_id"]):
                log_requests(con, REQ_PER_RECOVER, outcome="prewarm")
                stats["req"] += REQ_PER_RECOVER
                state["since_warm"] = 0
                state["clean"] += 1
                if state["clean"] >= 2 and state["window"] < FETCH_WINDOW:
                    state["window"] += 1
                    state["clean"] = 0
                await asyncio.sleep(random.uniform(3.0, 5.0))

        rec = await fetch_job(page, r["source_job_id"])
        state["since_warm"] += 1
        stats["req"] += REQ_PER_JOB
        stats["fetch"] += 1
        ok = bool(rec.get("ok"))
        log_requests(con, REQ_PER_JOB, outcome="ok" if ok else "challenged")
        if not ok:
            stats["challenged"] += 1

        if not ok and rec.get("reason") == "challenged":
            state["window"] = max(MIN_WINDOW, state["window"] - 1)
            state["clean"] = 0
            if await warm_stoken(page, ctx, r["source_job_id"]):
                log_requests(con, REQ_PER_RECOVER, outcome="rewarm")
                stats["req"] += REQ_PER_RECOVER
                state["since_warm"] = 0
                await asyncio.sleep(random.uniform(3.0, 5.0))
                rec = await fetch_job(page, r["source_job_id"])
                stats["req"] += REQ_PER_JOB
                stats["fetch"] += 1
                ok = bool(rec.get("ok"))
                log_requests(con, REQ_PER_JOB,
                             outcome="ok" if ok else "challenged")
                if not ok:
                    stats["challenged"] += 1
                    # 重暖后仍被挡 —— 这是 stoken 跟不上的硬信号
                    double_fail += 1

        # 落库（与生产同语义：只在判定成立时写）
        if ok:
            verdict = is_closed_status(rec.get("job_status"))
            if verdict is not None and same_job(rec.get("h1"), r["title"]):
                stats["judged"] += 1
                if verdict:
                    con.execute(
                        "UPDATE jobs SET job_invalid=1, "
                        "job_status_desc='职位已关闭', retention_status='sample', "
                        "updated_at=CURRENT_TIMESTAMP WHERE canonical_id=? "
                        "AND retention_status='active'", (r["canonical_id"],))
                    con.commit()
                    stats["closed"] += 1
                else:
                    phrase, days = parse_active_status(rec.get("active_time") or "")
                    if phrase:
                        con.execute(
                            "UPDATE jobs SET recruiter_active_status=?, "
                            "recruiter_online=?, updated_at=CURRENT_TIMESTAMP "
                            "WHERE canonical_id=?",
                            (phrase, 1 if phrase == "在线" else 0,
                             r["canonical_id"]))
                        con.commit()
                        stats["refreshed"] += 1

        await asyncio.sleep(random.uniform(*gap))

    mins = max((datetime.now() - t0).total_seconds() / 60, 0.01)
    return {
        "gap": list(gap), "jobs": len(rows), "requests": stats["req"],
        "fetches": stats["fetch"], "challenged": stats["challenged"],
        "judged": stats["judged"], "closed": stats["closed"],
        "refreshed": stats["refreshed"], "double_fail": double_fail,
        "minutes": round(mins, 1),
        "req_per_job": round(stats["req"] / max(len(rows), 1), 2),
        "req_per_min": round(stats["req"] / mins, 1),
        "jobs_per_hour": round(len(rows) / mins * 60),
        "challenge_rate": round(stats["challenged"] / max(stats["fetch"], 1), 3),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-step", type=int, default=12, help="每档跑多少条")
    ap.add_argument("--daily-cap", type=int, default=1000)
    ap.add_argument("--out", default="data/calibration/refresh-speed-ladder.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    need = args.per_step * len(LADDER) + 10
    rows = con.execute(
        "SELECT canonical_id, title, company_name, source_job_id FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT ?",
        (need,)).fetchall()
    print(f"候选 {len(rows)} 条，每档 {args.per_step} 条，共 {len(LADDER)} 档\n")

    # 开跑前体检：任一出口已被标记就不该做加速探测
    healthy = []
    for tag, port in EXITS:
        v, note = exit_status(port)
        print(f"[出口] {tag}(:{port}) {v} — {note}")
        if v == "ok":
            healthy.append((tag, port))
    if not healthy:
        print("没有健康出口，加速探测无从进行。")
        return
    print(f"健康出口: {[t for t,_ in healthy]}\n")

    results = []
    stop_reason = "跑完全部档位仍未触发停止判据"
    cursor = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])

        async def new_ctx(idx):
            tag, port = healthy[idx % len(healthy)]
            c = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                proxy={"server": f"socks5://127.0.0.1:{port}"})
            p = c.pages[0] if c.pages else await c.new_page()

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
            await p.route("**/*", _route)
            return c, p, tag, port

        ei = 0
        ctx, page, tag, port = await new_ctx(ei)
        if not await warm_stoken(page, ctx, rows[0]["source_job_id"]):
            print("冷启动拿不到 stoken，退出。")
            await ctx.close(); await browser.close(); return
        log_requests(con, REQ_PER_RECOVER, outcome="warm")
        print(f"[stoken] 冷启动就绪（出口 {tag}）\n")
        state = {"since_warm": 0, "window": FETCH_WINDOW, "clean": 0}

        for gap in LADDER:
            used = used_today(con)
            room = args.daily_cap - used
            need_req = int(args.per_step * 2.5)   # 按最差 2.5 次/条预留
            if room < need_req:
                stop_reason = f"日线余量不足（剩 {room} 次，本档约需 {need_req}）"
                print(f"\n⛔ {stop_reason}")
                break
            batch = rows[cursor:cursor + args.per_step]
            cursor += args.per_step
            if len(batch) < args.per_step:
                stop_reason = "候选岗位用尽"
                print(f"\n⛔ {stop_reason}")
                break

            print(f"── 档位 条间 {gap[0]:.0f}-{gap[1]:.0f}s，{len(batch)} 条 "
                  f"（今日已用 {used}/{args.daily_cap}）", flush=True)
            st = await run_step(page, ctx, con, batch, gap, state)
            results.append(st)
            print(f"   {st['req_per_job']} 次/条  {st['req_per_min']} 请求/分  "
                  f"{st['jobs_per_hour']} 条/小时  挑战率 {st['challenge_rate']:.0%}"
                  f"  判定 {st['judged']}/{st['jobs']}"
                  f"  关闭{st['closed']} 活跃{st['refreshed']}", flush=True)

            # ── 停止判据 ──
            if st["challenge_rate"] > CHALLENGE_RATE_STOP:
                stop_reason = (f"挑战率 {st['challenge_rate']:.0%} 超过 "
                               f"{CHALLENGE_RATE_STOP:.0%}，窗口在塌")
                print(f"   ⛔ {stop_reason}")
                break
            if st["double_fail"] >= DOUBLE_FAIL_STOP:
                stop_reason = (f"重暖后仍被挡 {st['double_fail']} 次，"
                               f"stoken 跟不上这个速率")
                print(f"   ⛔ {stop_reason}")
                break
            bad = []
            for t, p in healthy:
                v, note = exit_status(p)
                print(f"   [体检] {t} {v} — {note}", flush=True)
                if v != "ok":
                    bad.append((t, v, note))
            if bad:
                stop_reason = f"出口被标记: {bad}"
                print(f"   ⛔ {stop_reason}")
                results[-1]["exit_flagged"] = bad
                break
            # 换出口再进下一档，避免单个 IP 承担全部加速压力
            await ctx.close()
            ei += 1
            ctx, page, tag, port = await new_ctx(ei)
            if not await warm_stoken(page, ctx, rows[cursor]["source_job_id"]
                                     if cursor < len(rows) else batch[-1]["source_job_id"]):
                stop_reason = f"出口 {tag} 暖不起来"
                print(f"   ⛔ {stop_reason}")
                break
            log_requests(con, REQ_PER_RECOVER, outcome="warm")
            state = {"since_warm": 0, "window": FETCH_WINDOW, "clean": 0}
            print(f"   [出口] 换到 {tag}，进入下一档\n", flush=True)

        await ctx.close()
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"stop_reason": stop_reason, "steps": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"{'条间':<12}{'次/条':<8}{'请求/分':<9}{'条/小时':<9}{'挑战率':<8}{'判定'}")
    for s in results:
        print(f"{s['gap'][0]:.0f}-{s['gap'][1]:.0f}s{'':<6}"
              f"{s['req_per_job']:<8}{s['req_per_min']:<9}{s['jobs_per_hour']:<9}"
              f"{s['challenge_rate']:.0%}{'':<4}{s['judged']}/{s['jobs']}")
    print(f"\n停止原因: {stop_reason}")
    ok_steps = [s for s in results if s["challenge_rate"] <= CHALLENGE_RATE_STOP
                and not s.get("exit_flagged")]
    if ok_steps:
        best = max(ok_steps, key=lambda s: s["jobs_per_hour"])
        print(f"最快的安全档: 条间 {best['gap'][0]:.0f}-{best['gap'][1]:.0f}s "
              f"→ {best['jobs_per_hour']} 条/小时、{best['req_per_min']} 请求/分、"
              f"{best['req_per_job']} 次/条")
    print(f"结果已存 {out}")
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
