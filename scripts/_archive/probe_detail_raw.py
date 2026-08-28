"""诊断用：发一次详情请求，原样打印响应，不做风控判定也不重试。

目的是区分两种假设：
  A. 详情接口按请求数节流（那么第一次就该成功）
  B. 库里存的 securityId/lid 已过期（那么无论隔多久，第一次就该失败）
只发 1 个请求，命中什么都不重试。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import async_playwright

ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DB = Path(__file__).resolve().parents[1] / "data" / "hangzhou_agent.sqlite3"

FETCH = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const t = await r.text();
    return {status: r.status, text: t};
}"""


def pick_row(order: str) -> dict:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "select source_job_id, title, first_seen_at, raw_json from jobs "
        "where (job_description is null or job_description='') "
        f"order by first_seen_at {order} limit 1"
    ).fetchone()
    db.close()
    raw = json.loads(row["raw_json"])
    return {
        "job_id": row["source_job_id"],
        "title": row["title"],
        "first_seen_at": row["first_seen_at"],
        "securityId": raw.get("securityId") or "",
        "lid": raw.get("lid") or "",
    }


async def main() -> None:
    target = pick_row(sys.argv[1] if len(sys.argv) > 1 else "desc")
    print(f"目标: {target['title']} / 入库时间 {target['first_seen_at']}")
    print(f"securityId 前 24 位: {target['securityId'][:24]}...  lid: {target['lid']}")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if ORIGIN in (pg.url or "")), None)
        if page is None:
            print("没有已登录的 zhipin 页签，先在浏览器里打开一次")
            return
        print(f"复用页签: {page.url}")

        url = f"{ORIGIN}{DETAIL_PATH}?" + urlencode(
            {"securityId": target["securityId"], "lid": target["lid"]}
        )
        result = await page.evaluate(FETCH, url)
        print(f"\nHTTP {result['status']}  响应长度 {len(result['text'])}")
        print("--- 响应前 800 字符 ---")
        print(result["text"][:800])
        try:
            body = json.loads(result["text"])
        except json.JSONDecodeError:
            print("\n(非 JSON)")
            return
        print(f"\ncode={body.get('code')} message={body.get('message')!r}")
        zp = body.get("zpData") or {}
        desc = ((zp.get("jobInfo") or {}).get("postDescription") or "")
        print(f"zpData 顶层键: {list(zp.keys())}")
        print(f"postDescription 长度: {len(desc)}")


asyncio.run(main())
