"""连续请求 + code=37 自动 iv8 重算：验证 stoken 刷新后能否持续采集。

策略：
1. 抓一次最新 cookie + token
2. 循环请求详情（间隔 delay 秒），每轮请求用最新 stoken
3. 遇 code=37：从响应取 seed/ts/name，iv8 重算 stoken，更新 cookie + zp_token，重试该条
4. 遇其他风控（32/36/访问频繁）：停止
5. 记录每个请求的结果，统计「重算后成功」的次数

用法: python scripts/probe_stoken_loop.py [--count N] [--delay S]
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

from job_radar.stoken import compute_stoken  # noqa: E402
from job_radar.traceid import generate as gen_traceid  # noqa: E402

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

RISK_TEXTS = ("访问频繁", "安全校验", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")

HEADERS = {
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
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--delay", type=float, default=20.0)
    args = parser.parse_args()

    targets = pick(args.count)
    if not targets:
        print("无目标")
        return

    import asyncio
    import urllib.request

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
                    await asyncio.sleep(3)
                    continue
                if token:
                    break
            cookies = await ctx.cookies()
            jar = {c["name"]: c["value"] for c in cookies}
            if not jar.get("__zp_stoken__"):
                raise SystemExit("!! 没抓到 __zp_stoken__")
            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            return cookie_str, token

    cookie_str, token = asyncio.run(grab())
    print(f"[抓取] token={token[:16]}... __zp_stoken__="
          f"{dict(p.split('=',1) for p in cookie_str.split(';') if '=' in p).get('__zp_stoken__','')[:16]}...")

    class Session:
        def get(self, url_, timeout=15):
            req = urllib.request.Request(url_, headers={"User-Agent": HEADERS["User-Agent"]})
            return urllib.request.urlopen(req, timeout=timeout)

    sess = Session()

    def do_request(url_: str) -> dict:
        nonlocal cookie_str
        jar = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p)
        headers = dict(HEADERS)
        headers["Cookie"] = cookie_str
        headers["token"] = token
        if jar.get("bst"):
            headers["zp_token"] = jar["bst"]
        headers["traceid"] = gen_traceid()
        req = urllib.request.Request(url_, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8")}

    ok = recomputed = 0
    for i, (title, sid, lid) in enumerate(targets, start=1):
        url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
        try:
            res = do_request(url)
        except Exception as exc:
            print(f"[{i}] 请求异常: {exc}")
            break

        status, raw = res["status"], res["text"]
        hit = next((t for t in RISK_TEXTS if t in raw), None)
        if hit:
            print(f"[{i}] FAIL 风控「{hit}」: {raw[:120]}")
            break
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{i}] 非 JSON: {raw[:120]}")
            break
        code = body.get("code")

        if code == 37:
            zp = body.get("zpData") or {}
            seed, ts, name = zp.get("seed"), zp.get("ts"), zp.get("name")
            print(f"[{i}] code=37 name={name} → iv8 重算...")
            new_stoken = compute_stoken(seed, int(ts), name, sess)
            if not new_stoken:
                print(f"[{i}]   iv8 失败，停止")
                break
            jar = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p)
            jar["__zp_stoken__"] = new_stoken
            jar["bst"] = new_stoken
            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            recomputed += 1
            time.sleep(2)
            # 重试
            try:
                res2 = do_request(url)
            except Exception as exc:
                print(f"[{i}]   重试请求异常: {exc}")
                break
            body2 = json.loads(res2["text"])
            code2 = body2.get("code")
            jd2 = (((body2.get("zpData") or {}).get("jobInfo") or {}).get("postDescription") or "")
            if code2 == 0 and jd2:
                print(f"[{i}] ✅ 重算后成功 JD={len(jd2)} {title[:24]}")
                ok += 1
            else:
                print(f"[{i}]   重算后仍异常 code={code2}: {res2['text'][:100]}")
                break
        else:
            jd = (((body.get("zpData") or {}).get("jobInfo") or {}).get("postDescription") or "")
            if code == 0 and jd:
                print(f"[{i}] OK JD={len(jd)} {title[:24]}")
                ok += 1
            else:
                print(f"[{i}] 异常 code={code}: {raw[:100]}")
                break

        if i < len(targets) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\n成功 {ok}/{len(targets)}，其中重算后成功 {recomputed and '是' or '否'}")


if __name__ == "__main__":
    main()
