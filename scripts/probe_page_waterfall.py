"""量一条 job_detail 页真实发出多少次 HTTP 请求，以及拦掉静态资源后还剩多少。

**为什么要量**：`refresh_status_loop.REQ_PER_JOB = 14` 这个数是从一张 **verify 页**
数出来的（13 个请求，取 14 作保守估计），而 verify 页几乎没有内容。真正的岗位页
只会更多，而账本里 `detail_page` 一行都没有 —— 这条链路自从接入账本后从未实跑过，
所以「一条 14 次」这个贯穿全项目的成本假设**从来没有被岗位页验证过**。
它决定了日线换算、每轮条数、以及「这条链路比采集链路贵 14 倍」这个结论本身。

**三档对比**（同一个岗位，各用全新上下文，让 JS 挑战每次都真的重跑一遍）：
  A 不拦     —— 基线，真实成本
  B 拦图片/字体/媒体 + logapi 埋点 + geetest
  C 在 B 之上再拦 stylesheet

**C 档单独列出来是因为它有正确性风险**：`innerText` 受 CSS 可见性影响，
拦掉样式表会让本该 `display:none` 的模板节点变可见 —— 万一页面里藏着
「职位已关闭」这类模板文案，就会把在招岗位误判成已关闭。所以三档必须
**对比解析结果**而不只对比请求数：省下的请求不值得用误判换。

⚠️ 全程免登录、不带任何账号 cookie。只读 3 次公开岗位页，
住宅 IP 上匿名浏览岗位页是平台最常见的流量形态。
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

# 拦截判据。**必须放行 script/xhr/document**：BOSS 的 JS 挑战自己算
# `__zp_stoken__`，拦掉脚本等于让挑战永远过不去（无头模式挑战不自解已经是
# 一次同形态的教训，见项目记忆）。
BLOCK_TYPES_B = {"image", "media", "font"}
BLOCK_TYPES_C = BLOCK_TYPES_B | {"stylesheet"}
# 按域拦：logapi 是 BOSS 自己的埋点上报（主动报告行为特征），
# geetest 只在需要人点的验证页才真正用得上。
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "?").lower()


async def run_variant(browser, job_id: str, name: str,
                      block_types: set[str], block_hosts: tuple[str, ...]) -> dict:
    """跑一档，返回请求统计与解析结果。每档都用全新 context。"""
    ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
    sent: list[tuple[str, str]] = []      # (resource_type, host)
    blocked: list[tuple[str, str]] = []

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    if block_types or block_hosts:
        async def handler(route):
            req = route.request
            rt, h = req.resource_type, _host(req.url)
            if rt in block_types or any(b in h for b in block_hosts):
                blocked.append((rt, h))
                await route.abort()
            else:
                sent.append((rt, h))
                await route.continue_()
        await page.route("**/*", handler)
    else:
        page.on("request", lambda r: sent.append((r.resource_type, _host(r.url))))

    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        print(f"  [{name}] goto 异常（挑战跳转常在超时后才完成，继续等）: "
              f"{type(exc).__name__}")

    # 等挑战自解 + 正文填充。判据与 refresh_status_loop.load_and_read 一致：
    # 只认「标题含招聘」，因为「Loading …_security_check=…」这种中间态既不含
    # 「请稍候」也不含「验证」，排除法会误认为已通过。
    loop = asyncio.get_event_loop()
    start = loop.time()
    title = ""
    landed = ""
    while loop.time() - start < 45:
        cur = page.url or ""
        if "/verify.html" in cur:
            landed = "verify"
            break
        if "/403.html" in cur or "code=31" in cur:
            landed = "blocked"
            break
        try:
            title = await page.title()
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if title.strip() and "招聘" in title and "注册登录" not in title:
            landed = "ok"
            break
        await asyncio.sleep(1.5)

    text = ""
    if landed == "ok":
        for _ in range(10):
            try:
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if len(text) > 500:
                break
            await asyncio.sleep(0.8)

    phrase, days = parse_active_status(text) if text else (None, None)
    # 页面稳定后再等一拍，把「首屏之后才发的」请求（埋点、懒加载）也计进来 ——
    # 只数到 domcontentloaded 会系统性低估成本。
    await asyncio.sleep(3.0)
    await ctx.close()

    return {
        "variant": name,
        "landed": landed,
        "title": title[:60],
        "text_len": len(text),
        "closed": _is_closed(text) if text else None,
        "active_phrase": phrase,
        "active_days": days,
        "sent": len(sent),
        "blocked": len(blocked),
        "by_type": dict(Counter(t for t, _ in sent).most_common()),
        "by_host": dict(Counter(h for _, h in sent).most_common()),
        "blocked_by_host": dict(Counter(h for _, h in blocked).most_common()),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="", help="留空则取库里快照最旧的一条")
    ap.add_argument("--gap", type=float, default=25.0, help="档间间隔秒")
    ap.add_argument("--out", default="data/calibration/page-waterfall.json")
    args = ap.parse_args()

    job_id = args.job_id
    if not job_id:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT source_job_id, title, recruiter_active_status FROM jobs "
            "WHERE retention_status='active' AND job_invalid=0 "
            "  AND job_status_desc != '职位已关闭' AND job_description != '' "
            "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
            "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT 1").fetchone()
        con.close()
        job_id = r["source_job_id"]
        print(f"取库里最旧快照: {r['title'][:30]} | 库存活跃度 {r['recruiter_active_status']}")
    print(f"探测岗位 {job_id}\n")

    variants = [
        ("A-不拦", set(), ()),
        ("B-拦图片字体媒体+埋点", BLOCK_TYPES_B, BLOCK_HOSTS),
        ("C-B再拦样式表", BLOCK_TYPES_C, BLOCK_HOSTS),
    ]
    results = []
    async with async_playwright() as pw:
        # 必须有头：无头下 BOSS 的 JS 挑战不自解，每条都会「未判定」。
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        for i, (name, bt, bh) in enumerate(variants):
            print(f"[{name}] 开始…", flush=True)
            res = await run_variant(browser, job_id, name, bt, bh)
            results.append(res)
            print(f"  落点={res['landed']}  发出={res['sent']} 拦掉={res['blocked']}  "
                  f"正文={res['text_len']}字  关闭={res['closed']}  "
                  f"活跃度={res['active_phrase']}")
            print(f"  按类型 {res['by_type']}")
            print(f"  按域   {res['by_host']}", flush=True)
            if i < len(variants) - 1:
                print(f"  （间隔 {args.gap:.0f}s）\n", flush=True)
                await asyncio.sleep(args.gap)
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"job_id": job_id, "variants": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 64)
    base = results[0]["sent"]
    for r in results:
        cut = f"  省 {(1 - r['sent']/base)*100:.0f}%" if base and r is not results[0] else ""
        print(f"{r['variant']:<24} 请求 {r['sent']:>3}  落点 {r['landed']:<8} "
              f"正文 {r['text_len']:>5}字  活跃度 {str(r['active_phrase']):<6}{cut}")
    print(f"\n对照：refresh_status_loop.REQ_PER_JOB 当前写死 14")
    print(f"结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
