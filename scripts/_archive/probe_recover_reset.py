"""探针：验证「主动 security-check 重算」是否能重置 cookie 配额窗口。

假设（基于 probe_detail_header 发现）：
- 无 token detail 窗口 = 4 条，第 5 条 code=37
- security-check 重算 stoken 会重置 cookie 配额
- 若为真 → 最优节奏 = 4条/批 + 主动重算（~10s），替代 4条/批 + 长冷却

方法：
1. 无 token 连发 4 条（全 code=0），第 5 条触发 code=37，解析 seed/name/ts
2. 导航 security-check.html 重算（复用 _recover_code37）
3. 重算后再连发 4 条，看是否又是 4 条 code=0 → 窗口被重置
4. 重复 2-3 轮，验证「4条+重算」循环可持续

用法: python scripts/probe_recover_reset.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import (  # noqa: E402
    BossCollector, ORIGIN, API_PATH, DETAIL_PATH,
)
from job_radar.config import load_config  # noqa: E402

FETCH_TPL = """(payload) => fetch(payload.url, {credentials:'include'})
    .then(r => r.json()).catch(e => ({error: String(e)}))"""


async def send_n(page, jobs, start, n) -> tuple[int, int, dict | None]:
    """从 start 开始连发 n 条 detail，返回 (成功数, code37 触发时的 challenge 或 None)。"""
    ok = 0
    challenge = None
    for i in range(n):
        job = jobs[start + i]
        url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': job.get('securityId'), 'lid': job.get('lid')})}"
        body = await page.evaluate(FETCH_TPL, {"url": url})
        code = body.get("code") if isinstance(body, dict) else "?"
        if code == 0:
            ok += 1
            print(f"  [batch] {start+i+1:2d} code=0", flush=True)
        elif code == 37:
            zp = body.get("zpData") or {}
            challenge = {"seed": zp.get("seed"), "name": zp.get("name"), "ts": zp.get("ts")}
            print(f"  [batch] {start+i+1:2d} code=37 → challenge 捕获", flush=True)
            break
        else:
            print(f"  [batch] {start+i+1:2d} code={code}", flush=True)
            break
    return ok, (ok + 1 if challenge else ok), challenge  # ok条成功, 位置索引


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    from patchright.async_api import async_playwright

    profile = str(Path(cfg.browser.user_data_dir).resolve())
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={cfg.cities[0].boss_code}",
            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        collector = BossCollector(cfg)
        collector.context = ctx
        collector.page = page
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else None
        params = collector._params(city, cfg.keywords[0], filters, 1)
        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
        result = await page.evaluate(FETCH_TPL, {"url": url})
        jobs = ((result.get("zpData") or {}).get("jobList") or [])
        print(f"取到 {len(jobs)} 个 job", flush=True)
        if len(jobs) < 20:
            await ctx.close()
            return

        pos = 0
        windows = []
        for round_no in range(1, 9):  # 8 轮循环，观察窗口是否递减/升级
            print(f"\n=== 第 {round_no} 轮: 连发直到 code=37 ===", flush=True)
            ok_in_round = 0
            challenge = None
            for i in range(pos, pos + 10):  # 每轮最多试 10 条
                if i >= len(jobs):
                    break
                job = jobs[i]
                url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': job.get('securityId'), 'lid': job.get('lid')})}"
                body = await page.evaluate(FETCH_TPL, {"url": url})
                code = body.get("code") if isinstance(body, dict) else "?"
                if code == 0:
                    ok_in_round += 1
                    print(f"  [r{round_no}] {i+1:2d} code=0", flush=True)
                elif code == 37:
                    zp = body.get("zpData") or {}
                    challenge = {"seed": zp.get("seed"), "name": zp.get("name"), "ts": zp.get("ts")}
                    print(f"  [r{round_no}] {i+1:2d} code=37 → challenge name={challenge['name']}", flush=True)
                    break
                else:
                    print(f"  [r{round_no}] {i+1:2d} code={code} (非37，注意！)", flush=True)
                    break
            windows.append(ok_in_round)
            print(f"  本轮窗口 = {ok_in_round} 条 (累计 pos={pos + ok_in_round})", flush=True)
            pos += ok_in_round
            if challenge:
                print(f"  导航 security-check 重算 stoken...", flush=True)
                await collector._recover_code37(
                    str(challenge["seed"]), str(challenge["name"]), str(challenge["ts"]))
                print(f"  重算完成", flush=True)
            else:
                print(f"  本轮无 code=37（窗口>10 或配额未耗尽），提前结束", flush=True)
                break

        await ctx.close()
        print("\n=== 结论 ===", flush=True)
        print(f"每轮窗口: {windows}", flush=True)
        print(f"总 code=37/重算次数: {len(windows)} 次，窗口是否递减: "
              f"{'是(有风险)' if len(windows)>2 and windows[-1] < windows[0] else '否(稳定)'}", flush=True)
        print("若窗口稳定且无升级 → 4条+重算循环可持续；若递减 → 重算有累积惩罚，需控制频率", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
