"""列表扩充：用新筛选跑多关键词多页列表接口，扩充岗位池并刷新存活。

只抓列表接口（零风控），不抓详情。把符合目标画像的岗位 upsert 进库，
同时刷新 last_seen_at 确认存活。列表接口本身字段含薪资/公司/经验/学历，
足够做筛选和初筛。

用法: python scripts/expand_list.py [--pages N] [--delay S]
  --pages  每个关键词抓多少页（默认 5）
  --delay  每页间隔秒数（默认 8，列表接口零风控可放松）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.collectors.boss import BossCollector, ORIGIN, API_PATH  # noqa: E402
from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--delay", type=float, default=8.0)
    args = parser.parse_args()

    cfg = load_config("config.hangzhou.yaml")
    city = cfg.cities[0]
    filters = cfg.filters[0] if cfg.filters else None

    from patchright.async_api import async_playwright

    collector = BossCollector(cfg)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(Path(cfg.browser.user_data_dir).resolve()),
            channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        collector.context = ctx
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        collector.page = page
        await page.goto(f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={city.boss_code}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)
        print(f"页签: {page.url[:80]}")

        with JobStore(cfg.database_path) as store:
            from job_radar.throttle import HumanThrottle
            throttle = HumanThrottle(cfg)
            # 从页面读 token（会话内稳定），避免硬编码
            token = await collector._read_token()
            if not token:
                print("!! 读不到 token，退出")
                return
            seen_new = seen_total = 0
            for kw in cfg.keywords:
                for p in range(1, args.pages + 1):
                    # 请求前节流：高斯延迟 + 突发惩罚（账号信任度已降低，不能无脑快抓）
                    await throttle.delay_before_request("list")
                    params = collector._params(city, kw, filters, p)
                    url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
                    result = await page.evaluate(FETCH_TPL, {"url": url, "token": token})
                    status = int(result.get("status", 0))
                    text = result.get("text") or ""
                    hit = next((t for t in RISK_TEXTS if t in text), None)
                    if hit:
                        print(f"[{kw}|页{p}] 风控「{hit}」: {text[:100]}")
                        break
                    try:
                        body = json.loads(text or "{}")
                    except json.JSONDecodeError:
                        print(f"[{kw}|页{p}] 非 JSON: {text[:80]}")
                        break
                    code = body.get("code")
                    jobs = ((body.get("zpData") or {}).get("jobList") or [])
                    if not jobs:
                        print(f"[{kw}|页{p}] code={code} 无岗位，结束")
                        break
                    for rank, raw in enumerate(jobs, start=1):
                        job = collector._parse_api_job(raw, kw, rank)
                        if not collector._should_keep(job):
                            continue
                        status_up = store.upsert(job)
                        seen_total += 1
                        if status_up == "inserted":
                            seen_new += 1
                    print(f"[{kw}|页{p}] {len(jobs)} 条，本页保留后累计 {seen_total}，新增 {seen_new}")
                    # 关键词切换时额外随机停顿，模拟人翻完一类再换
                    if p < args.pages:
                        await page.wait_for_timeout(throttle._gaussian(args.delay, args.delay + 4) * 1000)

            print(f"\n列表扩充完成: 共处理 {seen_total} 条，新增 {seen_new} 条")
            await ctx.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
