"""工商信息富化：从 BOSS gongsi 页抓取公司工商登记信息。

对投递池里每家公司（有 brand_id 的），打开 https://www.zhipin.com/gongsi/{brand_id}.html，
从渲染后 DOM 提取 注册资本/成立时间/法定代表人/企业类型/经营状态/统一社会信用代码 等，
写入 jobs.company_reg_json（按 brand_id 更新该公司全部岗位）。

用独立干净 profile（--profile，默认 .browser-profile-reg），**无登录态、与郭辉账号无关**，
零账号风险。节奏 3-6s/家 + 每 20 家长停顿，防 IP 节流。

用法:
  python scripts/enrich_company_info.py --limit 10       # 试跑 10 家
  python scripts/enrich_company_info.py                  # 全量
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from patchright.async_api import async_playwright  # noqa: E402

from job_radar.refresh_fetch import (  # noqa: E402
    DEFAULT_WINDOW_POSITION, launch_args,
)
from job_radar.storage import JobStore  # noqa: E402

LABELS = [
    "注册资本", "注册地址", "营业期限", "法定代表人", "成立时间", "企业类型",
    "经营状态", "统一社会信用代码", "核准日期", "曾用名", "登记机关",
]
GONGSI = "https://www.zhipin.com/gongsi/{}.html"


def extract_reg(text: str) -> dict:
    """从 gongsi 页 innerText 提取工商字段。值取标签后第一行（关键字段均单行）。"""
    out = {}
    for lbl in LABELS:
        m = re.search(re.escape(lbl) + r"：\s*\n?\s*([^\n]+)", text)
        if m:
            out[lbl] = m.group(1).strip()
    return out


async def fetch_one(page, brand_id: str) -> dict:
    """打开一家公司 gongsi 页并提取工商信息。失败/无数据显示返回空 dict。"""
    try:
        await page.goto(GONGSI.format(brand_id), wait_until="domcontentloaded", timeout=30000)
        # 轮询等待工商信息渲染（SPA），最长 ~15s
        text = ""
        for _ in range(15):
            await page.wait_for_timeout(1000)
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            if "注册资本" in text or "暂无" in text:
                break
        if "注册资本" not in text:
            # 部分大厂工商块懒加载：滚动到底触发
            for _ in range(10):
                await page.evaluate("window.scrollBy(0, 1500)")
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(1500)
            text = await page.evaluate("document.body ? document.body.innerText : ''")
        return extract_reg(text)
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] {brand_id}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        return {}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--profile", default=".browser-profile-reg")
    parser.add_argument("--limit", type=int, default=0, help="只富化前 N 家（试跑用）")
    parser.add_argument("--brands", default="", help="只富化指定 brand_id（逗号分隔）")
    parser.add_argument("--force", action="store_true", help="已富化过的也重抓")
    parser.add_argument("--window-position", default=DEFAULT_WINDOW_POSITION,
                        help="浏览器窗口落点 x,y（默认 %(default)s，主屏之外不抢焦点）。"
                             "传空字符串则开在屏幕中央。⚠️ 坐标必须为正。")
    args = parser.parse_args()

    db_path = Path("data/hangzhou_agent.sqlite3")
    with JobStore(db_path) as store:
        rows = store.conn.execute(
            "SELECT brand_id, MAX(company_name) AS company_name, "
            "MAX(CASE WHEN company_reg_json != '{}' THEN 1 ELSE 0 END) AS done "
            "FROM jobs WHERE retention_status='active' AND brand_id != '' "
            "GROUP BY brand_id"
        ).fetchall()
    targets = [r for r in rows if args.force or not r["done"]]
    if args.brands:
        wanted = {b.strip() for b in args.brands.split(",") if b.strip()}
        targets = [r for r in targets if r["brand_id"] in wanted]
    already = len(rows) - len([r for r in rows if args.force or not r["done"]])
    if args.limit:
        targets = targets[: args.limit]
    print(f"待富化公司: {len(targets)} 家（共 {len(rows)} 家，已富化 {already}）", flush=True)
    if not targets:
        return

    profile = Path(args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(profile),
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=launch_args(args.window_position),
        ignore_default_args=["--enable-automation"],
    )
    page = await ctx.new_page()
    try:
        ok = 0
        empty = 0
        fail = 0
        started = time.time()
        for i, t in enumerate(targets, 1):
            brand_id = t["brand_id"]
            reg = await fetch_one(page, brand_id)
            if not reg:
                reg = await fetch_one(page, brand_id)  # 失败重试一次（临时网络/security-check）
            with JobStore(db_path) as store:
                if reg:
                    now = time.strftime("%Y-%m-%dT%H:%M:%S")
                    store.conn.execute(
                        "UPDATE jobs SET company_reg_json=?, company_reg_updated_at=? WHERE brand_id=?",
                        (json.dumps(reg, ensure_ascii=False), now, brand_id),
                    )
                    store.conn.commit()
                    ok += 1
                    if "注册资本" not in reg:
                        empty += 1
                else:
                    fail += 1
            if i % 10 == 0 or i == len(targets):
                el = (time.time() - started) / 60
                print(
                    f"[{i}/{len(targets)}] 成功 {ok} 空工商 {empty} 失败 {fail} | {el:.1f}min",
                    flush=True,
                )
            # 节奏：3-6s 高斯 + 每 20 家长停顿 15-30s
            delay = random.gauss(4.5, 1.0)
            delay = max(3.0, min(7.0, delay))
            if i % 20 == 0:
                delay += random.uniform(15, 30)
            await page.wait_for_timeout(int(delay * 1000))
    finally:
        await ctx.close()
        await pw.stop()
    print(f"完成: 成功 {ok}（含空工商 {empty}）/ 失败 {fail}，共 {len(targets)} 家", flush=True)


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
