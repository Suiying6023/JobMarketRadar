"""人工过「安全验证」和 JS 自解 security-check，是同一条逻辑吗？

**问题来源**：用户问「导航一次 security-check，人工过验证是不是也是这个逻辑」。
这个区分很实际 —— 它决定被标记的出口该怎么救：
  · 若同一条逻辑 → 人工点验证只是"手动触发一次 stoken 重算"，
    那么脚本自解就够了，弹窗给人点是多余的
  · 若两条不同逻辑 → 人工验证解决的是**另一层**（IP 信誉），
    stoken 重算解决不了它，反之亦然

**检索到的公开事实**（keenable，2026-08-30）：
`security-check.html` 里的核心是
`code = new ABC().z(seed, parseInt(ts) + (480 + new Date().getTimezoneOffset())*60*1000)`
—— 纯计算，输入是 URL 上的 seed/ts，**没有任何人机交互**。
而 `verify.html` 走的是极验（geetest）—— 需要点击/滑动，收集
`userresponse`（交互位置加密）、`passtime`（点击到完成的时间差）、`a`（轨迹压缩编码）。
极验官方 FAQ 还写明「验证模块生成后一定时间内未被交互会自动失效」。

**所以先验判断是：两者不是同一条逻辑。** 本探针用实测验证三件事：
  ① 正常态下 `security-check.html` 能否纯靠 JS 拿到新 stoken（不需人点）
  ② 那条链路上是否出现任何 geetest 资源（有 = 需要人机交互）
  ③ 两个页面各自加载了哪些域，形态差异是否支持"两层机制"的判断

⚠️ 免登录。只访问 security-check（正常态本来就会走的那一步）与读一条岗位页。
⚠️ **不主动去撞 verify.html**：那需要 IP 先被标记，而故意把出口打成 verify 态
   代价是等约 24 小时。verify 页的构成用**历史实测记录**（08-27 抓到的 13 个请求：
   `static.geetest.com` 3、`api.geetest.com` 2 …）对照，不重新制造一次。
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar.refresh_fetch import MIN_JOB_HTML_LEN  # noqa: E402

# 08-27 实测 verify.html 的请求构成，作为对照（不重新制造 verify 态）
HISTORIC_VERIFY_HOSTS = {
    "www.zhipin.com": 4, "static.geetest.com": 3, "api.geetest.com": 2,
    "static.zhipin.com": 2, "img.bosszhipin.com": 1, "logapi.zhipin.com": 1,
}


def _host(u: str) -> str:
    return (urlsplit(u).hostname or "?").lower()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/calibration/verify-vs-selfsolve.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT source_job_id, title FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY detail_fetched_at DESC LIMIT 1").fetchone()
    con.close()
    job_id = r["source_job_id"]

    sent: list[tuple[str, str]] = []
    report = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        # 本机直连、无 cookie、不带账号：只是普通匿名访客
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("request", lambda q: sent.append((q.resource_type, _host(q.url))))

        # ── ① 冷启动：整页加载一条岗位页，观察 security-check 是否自动介入 ──
        print("① 匿名首访岗位页（观察 security-check 是否自动介入、是否需人点）")
        url = f"https://www.zhipin.com/job_detail/{job_id}.html"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"   goto 异常（挑战跳转常在超时后完成）: {type(exc).__name__}")

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        urls_seen = []
        stoken = ""
        while loop.time() - t0 < 60:
            cur = page.url or ""
            if cur and (not urls_seen or urls_seen[-1] != cur):
                urls_seen.append(cur)
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            if "__zp_stoken__" in cookies:
                stoken = cookies["__zp_stoken__"]
                break
            await asyncio.sleep(1.0)

        hosts = Counter(h for _, h in sent)
        geetest_hits = sum(n for h, n in hosts.items() if "geetest" in h)
        report["selfsolve"] = {
            "got_stoken": bool(stoken),
            "stoken_len": len(stoken),
            "stoken_head": stoken[:40],
            "url_chain": urls_seen[:6],
            "requests": len(sent),
            "hosts": dict(hosts),
            "geetest_requests": geetest_hits,
            "human_interaction_needed": False,   # 全程没有任何点击操作
        }
        print(f"   拿到 stoken: {bool(stoken)}（{len(stoken)} 字符）")
        print(f"   URL 链: {' -> '.join(u.split('?')[0].rsplit('/',1)[-1] or u for u in urls_seen[:4])}")
        print(f"   请求 {len(sent)} 次，其中 geetest 相关 {geetest_hits} 次")
        print(f"   ⚠️ 全程**零点击** —— 若拿到了 stoken，说明这一层不需要人机交互")
        print(f"   各域: {dict(hosts)}\n")

        if not stoken:
            print("   没拿到 stoken，后面的对照无从做起。")
            await ctx.close(); await browser.close(); return

        # ── ② 主动导航 security-check.html，看它是否纯计算 ──
        # 用页面里现成的 seed/ts 不好拿（正常态不回 code=37），
        # 所以这里改成：清掉 stoken 再访问岗位页，观察它是否**自动**重走一遍。
        print("② 清掉 stoken cookie，再访问岗位页（观察是否自动重算，无需人点）")
        before = len(sent)
        await ctx.clear_cookies()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        t0 = loop.time()
        stoken2 = ""
        chain2 = []
        while loop.time() - t0 < 60:
            cur = page.url or ""
            if cur and (not chain2 or chain2[-1] != cur):
                chain2.append(cur)
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            if "__zp_stoken__" in cookies:
                stoken2 = cookies["__zp_stoken__"]
                break
            await asyncio.sleep(1.0)
        seg = sent[before:]
        hosts2 = Counter(h for _, h in seg)
        gt2 = sum(n for h, n in hosts2.items() if "geetest" in h)
        report["recheck_after_clear"] = {
            "got_stoken": bool(stoken2),
            "stoken_differs": bool(stoken2) and stoken2 != stoken,
            "requests": len(seg),
            "hosts": dict(hosts2),
            "geetest_requests": gt2,
            "saw_security_check": any("security-check" in u or "_security_check" in u
                                      for u in chain2),
            "url_chain": chain2[:6],
        }
        print(f"   重新拿到 stoken: {bool(stoken2)}，与旧值不同: "
              f"{bool(stoken2) and stoken2 != stoken}")
        print(f"   经过 security-check: {report['recheck_after_clear']['saw_security_check']}")
        print(f"   请求 {len(seg)} 次，geetest {gt2} 次")
        print(f"   ⚠️ 同样**零点击**\n")

        # ── ③ 验证新 stoken 真的能用（fetch 一条） ──
        print("③ 用新 stoken 做一次同源 fetch，确认它是有效配额")
        rec = await page.evaluate(
            """async ({url, minLen}) => {
                const r = await fetch(url, {credentials:'include'});
                const h = await r.text();
                return {status: r.status, len: h.length, ok: h.length >= minLen};
            }""", {"url": url, "minLen": MIN_JOB_HTML_LEN})
        report["fetch_with_new_stoken"] = rec
        print(f"   HTTP {rec.get('status')} 长度 {rec.get('len')} 可用={rec.get('ok')}\n")

        await ctx.close()
        await browser.close()

    report["historic_verify_page"] = {
        "hosts": HISTORIC_VERIFY_HOSTS,
        "note": "08-27 实测 verify.html 的构成，含 geetest 5 次；需人工点击",
    }

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("=" * 70)
    ss = report["selfsolve"]
    rc = report["recheck_after_clear"]
    print("【结论】")
    print(f"  security-check 自解: 拿到 stoken={ss['got_stoken']}，"
          f"geetest 请求={ss['geetest_requests']}，人工点击=0")
    print(f"  清 cookie 后重算  : 拿到 stoken={rc['got_stoken']}，"
          f"geetest 请求={rc['geetest_requests']}，人工点击=0")
    print(f"  历史 verify.html  : geetest 请求="
          f"{sum(n for h,n in HISTORIC_VERIFY_HOSTS.items() if 'geetest' in h)}，"
          f"**必须人工点击**")
    print()
    if ss["geetest_requests"] == 0 and rc["geetest_requests"] == 0:
        print("  → 两者**不是同一条逻辑**：")
        print("    stoken 重算是纯计算（seed/ts 进 MD5 类签名），零交互、几秒完成；")
        print("    verify.html 走极验，要收集点击轨迹与 passtime，必须真人。")
        print("    所以：stoken 用尽 ≠ IP 被标记，两者要用不同手段处理。")
    else:
        print("  → 出现了 geetest 请求，先验判断需要修正，看逐条明细。")
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
