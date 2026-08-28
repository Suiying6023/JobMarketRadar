"""路径1决定性验证：patchright persistent 页面内 fetch 采集。

对比 connect_over_cdp：patchright 直接启动浏览器（不连调试端口、不触发刷新循环）。
本脚本在页面内 fetch 详情接口 5 条，验证：
  1. 页面内 fetch 能持续跑（不触发 code=37/刷新循环）
  2. 用固定 token（8GJVqmbrjhi7vZ28，已确认跨会话固定）代替读 _PAGE.token

用法: python scripts/probe_patchright_collect.py [--count N] [--delay S]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ".browser-profile"
ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
FIXED_TOKEN = "8GJVqmbrjhi7vZ28"

FETCH_TPL = """(payload) => {
    const {url, token} = payload;
    return fetch(url, {
        credentials: 'include',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'token': token,
    }).then(async r => ({status: r.status, text: await r.text()}))
    .catch(e => ({status: -1, text: '', error: String(e)}));
}"""

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--delay", type=float, default=20.0)
    args = parser.parse_args()

    targets = pick(args.count)
    if not targets:
        print("无目标")
        return
    print(f"待测 {len(targets)} 条，间隔 {args.delay}s，固定 token={FIXED_TOKEN}")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        # 等页面稳定（_security_check 跳转完成）
        await page.wait_for_timeout(5000)
        print(f"页签: {page.url[:80]}")

        ok = fail = 0
        started = time.monotonic()
        for i, (title, sid, lid) in enumerate(targets, start=1):
            url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
            try:
                result = await page.evaluate(FETCH_TPL, {"url": url, "token": FIXED_TOKEN})
            except Exception as exc:
                print(f"[{i}] evaluate 异常: {exc}")
                fail += 1
                break

            status = int(result.get("status", 0))
            text = result.get("text") or ""
            elapsed = time.monotonic() - started

            hit = next((t for t in RISK_TEXTS if t in text), None)
            if hit:
                print(f"[{i}] +{elapsed:5.1f}s FAIL 风控「{hit}」: {text[:120]}")
                fail += 1
                break

            try:
                body = json.loads(text or "{}")
                code = body.get("code")
                zp = body.get("zpData") or {}
                desc_len = len(str((zp.get("jobInfo") or {}).get("postDescription") or ""))
            except json.JSONDecodeError:
                code, desc_len = "?", 0

            if status == 200 and code == 0 and desc_len > 0:
                print(f"[{i}] +{elapsed:5.1f}s OK  code=0 JD={desc_len} {title[:24]}")
                ok += 1
            else:
                print(f"[{i}] +{elapsed:5.1f}s FAIL HTTP {status} code={code} JD={desc_len}: {text[:120]}")
                fail += 1
                break

            # 每轮观察页面 URL 是否稳定（不刷新循环）
            try:
                u = page.url
            except Exception:
                u = "?"
            if "_security_check" in u or "jobs" not in u:
                print(f"[{i}] !! 页面 URL 变了: {u[:80]}")

            if i < len(targets) and args.delay > 0:
                await page.wait_for_timeout(args.delay * 1000)

        print(f"\n成功 {ok} 失败 {fail}")

        # 保持浏览器一会儿方便观察
        await page.wait_for_timeout(10_000)
        await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
