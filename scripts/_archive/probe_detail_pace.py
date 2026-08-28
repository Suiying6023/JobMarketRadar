"""测量详情接口的真实节流边界。

之前的结论「3-4 次就被风控」是在 detail_delay_seconds=[3,7] 下测出来的。
本脚本用可调间隔连续请求 N 条，把每次的 HTTP 状态、code、响应前缀原样打印，
失败时不抛异常继续跑完，用来区分：
  - 固定次数上限（次数一到就挂，跟间隔无关）
  - 频率窗口（拉长间隔就不挂）
命中账号级风控（code 36）立刻停止，不赌。

用法: python scripts/probe_detail_pace.py [条数] [最小间隔秒] [最大间隔秒]
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import async_playwright  # noqa: E402

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = Path(__file__).resolve().parents[1] / "data" / "hangzhou_agent.sqlite3"
OUT = Path(__file__).resolve().parents[1] / "data" / "probe_detail_pace.log"

FETCH = """async (url) => {
    try {
        const r = await fetch(url, {credentials: 'include'});
        return {status: r.status, text: await r.text()};
    } catch (e) {
        return {status: -1, text: '', error: String(e)};
    }
}"""


def pick(limit: int) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT title, raw_json FROM jobs
        WHERE (job_description IS NULL OR job_description = '')
        ORDER BY first_seen_at DESC
        """
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        sid, lid = raw.get("securityId"), raw.get("lid")
        if sid:
            out.append((r["title"], str(sid), str(lid or "")))
        if len(out) >= limit:
            break
    return out


async def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    lo = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    hi = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0

    targets = pick(count)
    print(f"待测 {len(targets)} 条，间隔 {lo}-{hi}s\n")

    log = OUT.open("w", encoding="utf-8")
    ok = fail = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            print("没有已登录的 zhipin 页签，先在浏览器里打开首页")
            return
        print(f"复用页签: {page.url}\n")

        started = time.monotonic()
        for i, (title, sid, lid) in enumerate(targets, start=1):
            url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
            res = await page.evaluate(FETCH, url)
            status = int(res.get("status", 0))
            text = res.get("text") or ""
            elapsed = time.monotonic() - started

            code: object = "?"
            desc_len = 0
            try:
                body = json.loads(text or "{}")
                code = body.get("code")
                zp = body.get("zpData") or {}
                desc_len = len(str((zp.get("jobInfo") or {}).get("postDescription") or ""))
            except json.JSONDecodeError:
                pass

            good = status == 200 and code == 0 and desc_len > 0
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            mark = "OK " if good else "FAIL"
            line = f"[{i:02d}] +{elapsed:6.1f}s {mark} HTTP {status} code={code} JD={desc_len} {title[:24]}"
            print(line)
            log.write(line + "\n")
            if not good:
                snippet = text[:300].replace("\n", " ")
                print(f"     body: {snippet}")
                log.write(f"     body: {snippet}\n")
            log.flush()

            if code == 36:
                print("\ncode=36 账号级风控，立刻停止")
                log.write("code=36 停止\n")
                break
            if i < len(targets):
                await asyncio.sleep(random.uniform(lo, hi))

    log.close()
    print(f"\n成功 {ok} 失败 {fail}，明细 {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
