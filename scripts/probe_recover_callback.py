"""重算 stoken 后，回哪个页面最省？—— 采集链路唯一还剩的实质优化点。

**现状**：`_recover_code37()` 重算完会导航回 `SEARCH_BASE`（完整搜索页 SPA），
理由写的是「恢复 fetch 所需的 zhipin 域环境」。实测那次导航很重：
整个重算真实上网 **139 次**，其中大头是 script 80 + xhr 45 —— 搜索页的壳。

**但 `_evaluate_fetch` 真正需要的只是「页面停在 zhipin 域」**
（`_settle_on_origin` 的判据就是 `ORIGIN in page.url`）。
它在页面里发同源 fetch，**不依赖搜索页的任何 DOM 或 JS 状态**。
那么回一个极轻的同域页面应该同样可用，而且省掉整个 SPA 的加载。

**候选**（都在 www.zhipin.com 域下）：
  · `SEARCH_BASE`（现状，完整 SPA）
  · `/web/common/security-check.html`（重算落地页本身，停在这就不再导航）
  · 一个 404/静态小页面
  · `about:` 不行 —— 会离开 zhipin 域，fetch 就不是同源了

**判据**：每种回法之后，① 页面是否仍在 zhipin 域 ② 同源 fetch 列表接口
是否成功（这是唯一真正要保住的能力）③ 这次导航发了多少请求。

⚠️ 用采集账号的持久化 profile，跑前必须确认没有其他采集进程
（Chrome SingletonLock）。本探针只发少量列表请求，不抓详情。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.collectors.boss import (  # noqa: E402
    API_PATH, ORIGIN, SEARCH_BASE, BossCollector,
)


def _host(u: str) -> str:
    return (urlsplit(u).hostname or "?").lower()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.cities3.yaml")
    ap.add_argument("--out", default="data/calibration/recover-callback.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    collection = cfg.collection if hasattr(cfg, "collection") else cfg
    c = BossCollector(collection)
    await c.start()

    sent: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    report: list[dict] = []

    try:
        page = c.page
        assert page is not None
        # abort 掉的请求同样触发 request 事件，真实上网 = requests - failed
        page.on("request", lambda q: sent.append((q.resource_type, _host(q.url))))
        page.on("requestfailed",
                lambda q: failed.append((q.resource_type, _host(q.url))))

        city = collection.cities[0]

        async def try_fetch() -> tuple[bool, int]:
            """在当前页面发一次同源列表 fetch，返回 (是否成功, 请求数)。"""
            mark, fmark = len(sent), len(failed)
            params = {"scene": "1", "query": "AI Agent", "city": city.boss_code,
                      "page": 1, "pageSize": 30}
            res = await c._evaluate_fetch(f"{ORIGIN}{API_PATH}?{urlencode(params)}")
            raw = res.get("text") or ""
            net = (len(sent) - mark) - (len(failed) - fmark)
            try:
                body = json.loads(raw or "{}")
                ok = bool((body.get("zpData") or {}).get("jobList"))
            except json.JSONDecodeError:
                ok = False
            return ok, net

        candidates = [
            ("搜索页 SEARCH_BASE（现状）",
             f"{SEARCH_BASE}?{urlencode({'query': 'AI Agent', 'city': city.boss_code})}"),
            ("security-check 落地页（不再导航）", None),
            ("轻量同域页 /about",  f"{ORIGIN}/about"),
        ]

        for label, url in candidates:
            print(f"── {label}")
            mark, fmark = len(sent), len(failed)
            if url:
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=collection.browser.navigation_timeout_ms)
                except Exception as exc:
                    print(f"   goto 异常: {type(exc).__name__}")
                await page.wait_for_timeout(1500)
            else:
                print("   （跳过导航，停在当前页）")
            seg, fseg = sent[mark:], failed[fmark:]
            nav_net = len(seg) - len(fseg)
            on_origin = ORIGIN in (page.url or "")
            ok, fetch_net = await try_fetch()
            rec = {
                "label": label, "url": url or "(no navigation)",
                "nav_attempts": len(seg), "nav_aborted": len(fseg),
                "nav_network": nav_net,
                "still_on_origin": on_origin,
                "fetch_ok": ok, "fetch_requests": fetch_net,
                "page_url": (page.url or "")[:80],
                "by_type": dict(Counter(t for t, _ in seg).most_common(6)),
            }
            report.append(rec)
            print(f"   导航真实上网 {nav_net} 次（发起 {len(seg)}、拦 {len(fseg)}）")
            print(f"   停在 zhipin 域: {on_origin}   同源 fetch 成功: {ok}")
            print(f"   {rec['by_type']}")
            print(f"   当前 URL: {rec['page_url']}\n", flush=True)
            await asyncio.sleep(4.0)
    finally:
        await c.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("=" * 72)
    print(f"{'回法':<34}{'导航请求':<10}{'同域':<7}{'fetch可用'}")
    for r in report:
        print(f"{r['label']:<34}{r['nav_network']:<10}"
              f"{str(r['still_on_origin']):<7}{r['fetch_ok']}")
    usable = [r for r in report if r["fetch_ok"] and r["still_on_origin"]]
    if usable:
        best = min(usable, key=lambda r: r["nav_network"])
        cur = report[0]
        print(f"\n最省的可用回法: {best['label']}（{best['nav_network']} 次）")
        if best is not cur and cur["nav_network"]:
            print(f"  相比现状省 {cur['nav_network'] - best['nav_network']} 次/重算"
                  f"（{(1 - best['nav_network']/cur['nav_network'])*100:.0f}%）")
    print(f"结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
