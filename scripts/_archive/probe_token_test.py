"""决定性测试：带 token 头的 fetch 能否突破无 token 时的 12-13 次配额上限。

背景：无 token 的 fetch（credentials:'include'）在 [15,25]s 间隔下连续 12 条成功、
第 13 次命中风控。而页面自己发的请求带 token 头（来源 window._PAGE.token）能持续成功。
本脚本验证：fetch 加上 token 头后，配额是否显著提高。

token 在登录会话内是稳定值（页面 3 次请求同一个 token），所以脚本开始时读一次即可，
之后循环复用。页面刷新会临时把 _PAGE 重置，读取时重试。

用法: python scripts/probe_token_test.py [请求数] [最小间隔] [最大间隔]
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "data" / "token_probe"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")

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
READ_TOKEN = """() => {
    if (typeof window._PAGE !== 'undefined' && window._PAGE && window._PAGE.token) {
        return window._PAGE.token;
    }
    // 兜底：Vue 深处 userInfo.value.token（路径复杂易变，仅作为备选）
    return '';
}"""


def pick(limit: int) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, raw_json FROM jobs "
        "WHERE (job_description IS NULL OR job_description = '') "
        "ORDER BY first_seen_at DESC LIMIT 300"
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if raw.get("securityId"):
            out.append((r["title"], str(raw["securityId"]), str(raw.get("lid") or "")))
        if len(out) >= limit:
            break
    return out


def log(line: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {line}", flush=True)


async def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    lo = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    hi = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0

    targets = pick(count)
    log(f"待测 {len(targets)} 条，间隔 {lo}-{hi}s")
    OUT.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright
    from playwright.async_api import Error as PWError

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            log("没有 zhipin 页签")
            return
        log(f"页签: {page.url}")

        # 读 token（页面可能正在刷新，重试几次）
        token = ""
        for attempt in range(1, 6):
            try:
                token = await page.evaluate(READ_TOKEN)
            except PWError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                token = ""
            if token:
                break
            log(f"读取 token 失败（可能页面刷新中），等待重试 {attempt}/5")
            await asyncio.sleep(4)

        if not token:
            log("!! 无法读取 token，中止")
            return
        log(f"token: {token[:20]}...")

        OUT.mkdir(parents=True, exist_ok=True)
        logfile = OUT / f"token-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        ok = fail = 0
        started = time.monotonic()

        async def evaluate_fetch(url: str, tok: str) -> dict:
            """发一次带 token 的 fetch，扛住 CDP 连接触发的页面刷新。"""
            for attempt in range(1, 4):
                try:
                    return await page.evaluate(FETCH_TPL, {"url": url, "token": tok})
                except PWError as exc:
                    if "Execution context was destroyed" not in str(exc) or attempt == 3:
                        raise
                    log(f"上下文被页面刷新销毁，等待 6s 重试 {attempt}/3")
                    await asyncio.sleep(6)
            raise RuntimeError("fetch 重试耗尽")  # pragma: no cover

        for i, (title, sid, lid) in enumerate(targets, start=1):
            url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
            result = await evaluate_fetch(url, token)
            status = int(result.get("status", 0))
            text = result.get("text") or ""
            elapsed = time.monotonic() - started

            hit = next((t for t in RISK_TEXTS if t in text), None)
            code: object = "?"
            desc_len = 0
            if not hit:
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
            log(line)
            with logfile.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if hit:
                log(f"     风控关键字「{hit}」，正文: {text[:120]!r}")
                break
            if i < len(targets):
                await asyncio.sleep(random.uniform(lo, hi))

    log(f"成功 {ok} 失败 {fail}，明细 {logfile}")


if __name__ == "__main__":
    asyncio.run(main())
