"""探测详情接口的节流冷却窗口。

用法: python scripts/probe_cooldown.py [请求数] [间隔分钟]

设计原则：每次只发 1 个请求就停下来判断，把请求数压到最低。
默认只发 1 次（纯探活：窗口是否已恢复）。给了间隔分钟才会连发多次。

时间戳全程记录到 data/cooldown_probe.log（追加，不覆盖），
因为冷却窗口的长度只能靠"命中时刻 → 恢复时刻"的差值反推，
跨进程、跨会话都要能拼起来。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.async_api import async_playwright  # noqa: E402

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
LOG = ROOT / "data" / "cooldown_probe.log"

FETCH = """async (url) => {
    try {
        const r = await fetch(url, {credentials: 'include'});
        return {status: r.status, text: await r.text()};
    } catch (e) {
        return {status: -1, text: '', error: String(e)};
    }
}"""

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


async def fetch_with_retry(page, url: str, attempts: int = 3) -> dict:
    """发同源请求，遇到导航导致的上下文销毁就等页面稳定后重试。

    页面 URL 会带 _security_check=... 跳一次，正好撞上 evaluate 时 Playwright 抛
    "Execution context was destroyed"。这不是节流，重试即可；不重试会白耗一次请求配额。
    """
    from playwright.async_api import Error as PlaywrightError

    for attempt in range(1, attempts + 1):
        try:
            return await page.evaluate(FETCH, url)
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                raise
            log(f"    上下文被导航销毁，等待后重试（第 {attempt} 次）")
            await asyncio.sleep(5)
    raise RuntimeError("重试耗尽")  # pragma: no cover


def pick(limit: int) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, raw_json FROM jobs "
        "WHERE (job_description IS NULL OR job_description = '') "
        "ORDER BY first_seen_at DESC LIMIT 200"
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
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


async def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    gap_min = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

    targets = pick(count)
    if not targets:
        log("没有可用的 securityId，退出")
        return
    log(f"=== 探针启动：计划 {len(targets)} 次，间隔 {gap_min} 分钟 ===")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            log("没有已登录的 zhipin 页签，先在浏览器里打开 https://www.zhipin.com/")
            return

        for i, (title, sid, lid) in enumerate(targets, start=1):
            url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
            res = await fetch_with_retry(page, url)
            status = int(res.get("status", 0))
            text = res.get("text") or ""

            hit = next((t for t in RISK_TEXTS if t in text), None)
            if hit:
                log(f"[{i}] 节流 HTTP {status} 命中「{hit}」 -> 窗口未恢复，停止")
                log("=== 下次探测请在若干分钟后再跑一次，用两次时间差反推窗口长度 ===")
                return

            try:
                body = json.loads(text or "{}")
            except json.JSONDecodeError:
                log(f"[{i}] 非 JSON 响应 HTTP {status}，前 120 字符: {text[:120]!r}")
                return

            zp = body.get("zpData") or {}
            desc_len = len(str((zp.get("jobInfo") or {}).get("postDescription") or ""))
            if desc_len > 0:
                log(f"[{i}] 恢复 HTTP {status} code={body.get('code')} JD={desc_len} {title[:26]}")
            else:
                log(f"[{i}] 异常 HTTP {status} code={body.get('code')} 无 jobInfo/JD，"
                    f"前 120 字符: {text[:120]!r}")
                return

            if i < len(targets) and gap_min > 0:
                log(f"    等待 {gap_min} 分钟")
                await asyncio.sleep(gap_min * 60)


if __name__ == "__main__":
    asyncio.run(main())
