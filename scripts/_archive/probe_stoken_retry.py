"""闭环验证：请求 → 遇 code=37 → iv8 重算 stoken → 重试。

这是路径 2 的分水岭测试。流程：
1. 用最新 cookie + token + traceid 发 1 条纯 HTTP 详情请求
2. 如果返回 code=37：从响应取 seed/ts/name，iv8 本地算新 __zp_stoken__，
   更新 cookie 里的 __zp_stoken__ 和 header 的 zp_token，重试同一请求
3. 报告重试是否成功

用法: python scripts/probe_stoken_retry.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sqlite3  # noqa: E402

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


def pick_one() -> tuple[str, str, str]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, raw_json FROM jobs "
        "WHERE (job_description IS NULL OR job_description = '') "
        "ORDER BY first_seen_at DESC LIMIT 1"
    ).fetchall()
    con.close()
    raw = json.loads(rows[0]["raw_json"] or "{}")
    return rows[0]["title"], str(raw["securityId"]), str(raw.get("lid") or "")


def main() -> None:
    import asyncio
    import urllib.request

    from playwright.async_api import async_playwright

    # 1) 连 CDP 抓最新 cookie + token
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
            print(f"[抓取] token={token[:16]}... cookie={len(jar)}个 "
                  f"__zp_stoken__={jar['__zp_stoken__'][:16]}...")
            return cookie_str, token

    cookie_str, token = asyncio.run(grab())
    bst = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p).get("bst", "")

    # 2) 发请求，遇 code=37 用 iv8 重算重试
    title, sid, lid = pick_one()
    url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
    print(f"目标: {title}\n")

    # Session 适配 iv8 的下载需求
    class Session:
        def get(self, url_, timeout=15):
            req = urllib.request.Request(url_, headers={"User-Agent": HEADERS["User-Agent"]})
            return urllib.request.urlopen(req, timeout=timeout)

    sess = Session()

    # 尝试至多 2 轮（1 次触发 + 1 次重试）
    for round_no in range(1, 3):
        headers = dict(HEADERS)
        headers["Cookie"] = cookie_str
        headers["token"] = token
        if bst:
            headers["zp_token"] = bst
        headers["traceid"] = gen_traceid()

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except Exception as exc:
            print(f"[轮{round_no}] 请求异常: {exc}")
            return

        hit = next((t for t in RISK_TEXTS if t in raw), None)
        if hit:
            print(f"[轮{round_no}] FAIL 风控关键字「{hit}」: {raw[:150]}")
            return

        body = json.loads(raw)
        code = body.get("code")
        jd = (((body.get("zpData") or {}).get("jobInfo") or {}).get("postDescription") or "")
        if code == 0 and jd:
            print(f"[轮{round_no}] ✅ 成功 HTTP {status} code=0 JD={len(jd)}")
            print("方案成立：iv8 重算 stoken 后请求成功")
            return

        if code == 37:
            zp = body.get("zpData") or {}
            seed, ts, name = zp.get("seed"), zp.get("ts"), zp.get("name")
            print(f"[轮{round_no}] code=37，seed={str(seed)[:16]}... name={name} ts={ts}")
            if not (seed and ts and name):
                print("  缺 seed/ts/name，无法重算")
                return
            new_stoken = compute_stoken(seed, int(ts), name, sess)
            if not new_stoken:
                print("  iv8 计算失败")
                return
            # 更新 cookie 里的 __zp_stoken__ 和 header 的 zp_token
            jar = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p)
            jar["__zp_stoken__"] = new_stoken
            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            bst = new_stoken
            print(f"  已重算 stoken: {new_stoken[:16]}...，重试")
            time.sleep(2)
            continue

        print(f"[轮{round_no}] 异常 code={code}: {raw[:150]}")
        return

    print("重试轮数耗尽")


if __name__ == "__main__":
    main()
