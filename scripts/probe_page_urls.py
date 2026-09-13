"""连续读多条岗位页，量**每条的边际成本**与 URL 级构成。

上一支探针（`probe_page_waterfall.py`）每档都用全新 context，所以每档都完整付了
一次 JS 挑战的代价，量到的是「冷启动一条 = 102 次」。但真实循环
（`refresh_status_loop`）是**同一个 context 连读 15 条**才换出口 ——
而 `__zp_stoken__` 是**会话级** cookie，第 2 条起挑战不必重跑。
所以生产形态下第 2..N 条的边际成本可能远低于第 1 条，
而全项目的成本模型（REQ_PER_JOB、日线换算、"贵 14 倍"）都建立在「每条同价」上。

**另一个必须分清的量**：上一支探针显示拦掉图片能把 102 砍到 39，但
`www.zhipin.com` 这个**受风控的域**从 21 只降到 20 —— 几乎没变。
如果风控数的是自家域的请求，那么「省 62%」省掉的是带宽和时间，**不是风险**。
所以这里按域分别计数，并打出 URL 级明细，才能判断哪些是真能省的。

⚠️ 免登录，只读公开岗位页。用 `--jobs` 控制条数（默认 4：1 条冷启 + 3 条边际）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import _is_closed, parse_active_status  # noqa: E402

BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com")
# 风控主体域。判「省下的是风险还是带宽」只能看这个域的计数。
RISK_HOST = "www.zhipin.com"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "?").lower()


def _short(url: str) -> str:
    """URL 去掉 query 里的长 token，留路径 + 前两个参数名，便于归类。"""
    p = urlsplit(url)
    q = p.query.split("&")
    keys = [kv.split("=")[0] for kv in q[:3] if kv]
    return f"{p.path}{'?' + ','.join(keys) if keys else ''}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4, help="连读多少条（第 1 条是冷启动）")
    ap.add_argument("--gap", type=float, default=20.0, help="条间间隔秒")
    ap.add_argument("--no-block", action="store_true", help="不拦静态资源（量原始构成）")
    ap.add_argument("--out", default="data/calibration/page-urls.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT ?",
        (args.jobs,)).fetchall()
    con.close()
    print(f"连读 {len(rows)} 条，拦静态资源={not args.no_block}\n")

    per_job = []
    all_urls: Counter = Counter()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        # **一个 context 连读全部**，复刻生产形态：stoken 是会话级的。
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        sent: list[tuple[str, str, str]] = []   # (type, host, short_url)
        blocked = 0

        if args.no_block:
            page.on("request", lambda r: sent.append(
                (r.resource_type, _host(r.url), _short(r.url))))
        else:
            async def handler(route):
                nonlocal blocked
                req = route.request
                rt, h = req.resource_type, _host(req.url)
                if rt in BLOCK_TYPES or any(b in h for b in BLOCK_HOSTS):
                    blocked += 1
                    await route.abort()
                else:
                    sent.append((rt, h, _short(req.url)))
                    await route.continue_()
            await page.route("**/*", handler)

        for i, r in enumerate(rows, 1):
            mark = len(sent)
            blocked_mark = blocked
            job_id = r["source_job_id"]
            url = f"https://www.zhipin.com/job_detail/{job_id}.html"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                print(f"  goto 异常（挑战跳转常在超时后完成，继续等）: {type(exc).__name__}")

            loop = asyncio.get_event_loop()
            start = loop.time()
            title = ""
            landed = ""
            while loop.time() - start < 45:
                cur = page.url or ""
                if "/verify.html" in cur:
                    landed = "verify"; break
                if "/403.html" in cur or "code=31" in cur:
                    landed = "blocked"; break
                try:
                    title = await page.title()
                except Exception:
                    await asyncio.sleep(1.0); continue
                if title.strip() and "招聘" in title and "注册登录" not in title:
                    landed = "ok"; break
                await asyncio.sleep(1.5)

            text = ""
            if landed == "ok":
                for _ in range(10):
                    try:
                        text = await page.evaluate(
                            "() => document.body ? document.body.innerText : ''")
                    except Exception:
                        await asyncio.sleep(1.0); continue
                    if len(text) > 500:
                        break
                    await asyncio.sleep(0.8)
            # 首屏之后的埋点/懒加载也要计进来
            await asyncio.sleep(3.0)

            mine = sent[mark:]
            risk_n = sum(1 for _, h, _ in mine if h == RISK_HOST)
            phrase, days = parse_active_status(text) if text else (None, None)
            rec = {
                "n": i, "job_id": job_id, "landed": landed,
                "sent": len(mine), "blocked": blocked - blocked_mark,
                "risk_host_sent": risk_n,
                "text_len": len(text),
                "closed": _is_closed(text) if text else None,
                "active_phrase": phrase,
                "by_type": dict(Counter(t for t, _, _ in mine).most_common()),
                "by_host": dict(Counter(h for _, h, _ in mine).most_common()),
                "risk_urls": dict(Counter(
                    u for _, h, u in mine if h == RISK_HOST).most_common()),
            }
            per_job.append(rec)
            for _, h, u in mine:
                all_urls[f"{h}{u}"] += 1
            tag = "冷启动" if i == 1 else "边际"
            print(f"[{i}/{len(rows)} {tag}] {r['title'][:22]}")
            print(f"  落点={landed} 发出={len(mine)} 拦={blocked - blocked_mark} "
                  f"其中 {RISK_HOST}={risk_n}  正文={len(text)}字 "
                  f"活跃度={phrase}（库存 {r['recruiter_active_status']}）")
            print(f"  {rec['by_type']}")
            print(f"  {RISK_HOST} 明细: {rec['risk_urls']}", flush=True)
            if i < len(rows):
                await asyncio.sleep(args.gap)

        await ctx.close()
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"blocked_static": not args.no_block, "per_job": per_job,
         "all_urls": all_urls.most_common()}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"{'条':<4}{'总请求':<8}{RISK_HOST:<20}{'正文':<8}{'落点'}")
    for r in per_job:
        print(f"{r['n']:<4}{r['sent']:<8}{r['risk_host_sent']:<20}"
              f"{r['text_len']:<8}{r['landed']}")
    if len(per_job) > 1:
        marg = per_job[1:]
        print(f"\n冷启动 {per_job[0]['sent']} 次"
              f"（风控域 {per_job[0]['risk_host_sent']}）")
        print(f"边际均值 {sum(r['sent'] for r in marg)/len(marg):.1f} 次"
              f"（风控域 {sum(r['risk_host_sent'] for r in marg)/len(marg):.1f}）")
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
