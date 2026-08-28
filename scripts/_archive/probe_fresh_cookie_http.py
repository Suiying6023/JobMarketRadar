"""验证「最新 cookie + 纯 HTTP」是否可行（分水岭测试的正确做法）。

之前纯 HTTP 单条成功、批量失败，怀疑是落盘 cookie 在抓取后因页面刷新而过期
（__zp_stoken__ 每次刷新/重登会换）。本脚本在同一进程内：连 CDP 抓最新 cookie
和 token → 立即用 urllib 发 1 条 detail 请求 → 报告结果。

这排除了「cookie 过期」这个变量，如果仍失败，那就是动态 seed 硬门槛
（__zp_sseed__ 每次请求动态生成，纯 HTTP 无法预生成）。

用法: python scripts/probe_fresh_cookie_http.py [--count N] [--delay S]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.traceid import generate as gen_traceid  # noqa: E402

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")

FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def pick(limit: int) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, raw_json FROM jobs "
        "WHERE (job_description IS NULL OR job_description = '') "
        "ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        if raw.get("securityId"):
            out.append((r["title"], str(raw["securityId"]), str(raw.get("lid") or "")))
        if len(out) >= limit:
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--delay", type=float, default=20.0)
    args = parser.parse_args()

    targets = pick(args.count)
    if not targets:
        print("无目标")
        return

    # 连 CDP 抓最新 cookie + token
    import asyncio

    from playwright.async_api import async_playwright

    async def grab() -> tuple[str, str]:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
            if page is None:
                raise SystemExit("无 zhipin 页签")
            from playwright.async_api import Error as PWError

            token = ""
            for _ in range(6):
                try:
                    token = str(await page.evaluate(
                        "() => (window._PAGE && window._PAGE.token) || ''"))
                except PWError:
                    token = ""
                    await asyncio.sleep(3)
                    continue
                if token:
                    break
            cookies = await ctx.cookies()
            jar = {c["name"]: c["value"] for c in cookies}
            if not jar.get("__zp_stoken__"):
                raise SystemExit("!! 没抓到 __zp_stoken__")
            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            print(f"[抓取] token={token[:16]}... cookie={len(jar)}个 "
                  f"__zp_stoken__={jar['__zp_stoken__'][:16]}...")
            return cookie_str, token

    cookie_str, token = asyncio.run(grab())

    # 立即 urllib 发请求（不再连 CDP）
    import urllib.request

    headers = dict(FETCH_HEADERS)
    headers["Cookie"] = cookie_str
    headers["token"] = token
    bst = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p).get("bst")
    if bst:
        headers["zp_token"] = bst

    ok = fail = 0
    for i, (title, sid, lid) in enumerate(targets, start=1):
        url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
        req_headers = dict(headers)
        req_headers["traceid"] = gen_traceid()
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except Exception as exc:
            print(f"[{i}] 请求异常: {exc}")
            fail += 1
            continue

        hit = next((t for t in RISK_TEXTS if t in raw), None)
        if hit:
            print(f"[{i}] FAIL 风控命中「{hit}」: {raw[:200]}")
            fail += 1
            break
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{i}] 非 JSON: {raw[:200]}")
            fail += 1
            continue
        code = body.get("code")
        jd = (((body.get("zpData") or {}).get("jobInfo") or {}).get("postDescription") or "")
        if code == 0 and jd:
            print(f"[{i}] OK HTTP {status} code=0 JD={len(jd)} {title[:30]}")
            ok += 1
        else:
            print(f"[{i}] 异常 HTTP {status} code={code} 无JD: {raw[:150]}")
            fail += 1
        if i < len(targets) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\n成功 {ok} 失败 {fail}")


if __name__ == "__main__":
    main()
