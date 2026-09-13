"""量采集侧 stoken 重算的**真实请求数**，并与刷新侧对照。

**为什么怀疑账不准**：`boss.py:_recover_code37()` 只调一次 `_count_request()`
（记 1 行 `security_check`），但它实际发**两次整页导航**：
  ① `security-check.html?seed=..&name=..&ts=..`
  ② 回 `SEARCH_BASE`（搜索页，恢复 fetch 所需的同源环境）
而采集侧**没有任何 `page.route` 拦截**（实测 `grep -c` = 0），
所以这两次导航的子资源（图片/CSS/JS/埋点）全都老实下载了。

刷新侧同一件事量过：整页岗位页 **102 次**请求，拦掉静态资源后约 10 次。
搜索页只会更重（它是 SPA 主页面）。**如果这里也是几十次，那么
「采集侧重算只值 1 次请求」这个判断是错的**，而它正是我先前
断言「采集侧不该照搬主动重暖」的依据 —— 依据错了，结论也要重算。

**做法**：在已登录 context 上挂一个纯观测的 `page.on("request")`，
真的触发一次重算，数两次导航各发多少请求、分别落在哪些域。

⚠️ 只跑**一次**重算，约 2 次导航。不抓任何详情，不占详情配额意义上的量，
但导航本身是真实请求，会记进账本（这正是要量的东西）。
⚠️ 用采集账号的持久化 profile —— 必须确认没有别的采集进程在跑
（Chrome SingletonLock 会冲突）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.collectors.boss import (  # noqa: E402
    API_PATH, ORIGIN, SEARCH_BASE, BossCollector,
)
from urllib.parse import urlencode  # noqa: E402


def _host(u: str) -> str:
    return (urlsplit(u).hostname or "?").lower()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.cities3.yaml")
    ap.add_argument("--out", default="data/calibration/recover-cost.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    collection = cfg.collection if hasattr(cfg, "collection") else cfg
    c = BossCollector(collection)
    await c.start()

    sent: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    report: dict = {}
    try:
        page = c.page
        assert page is not None
        # ⚠️ `page.on("request")` 对**被 route.abort() 掉的请求也会触发** ——
        # 它数的是"发起尝试"，不是真上网的次数。所以只用它会把拦截当成没生效
        # （我第一次就是这么误判的：拦截明明在工作，探针仍报 221 次）。
        # 正确做法是同时听 `requestfailed`（abort 会走这里），
        # **真实网络请求数 = requests - failed**。
        page.on("request", lambda q: sent.append((q.resource_type, _host(q.url))))
        page.on("requestfailed",
                lambda q: failed.append((q.resource_type, _host(q.url))))

        city = collection.cities[0]
        # ── 先到搜索页（这是采集的正常起点），顺便量这一步的成本 ──
        mark, fmark = len(sent), len(failed)
        await c._goto_search(
            f"{SEARCH_BASE}?{urlencode({'query': 'AI Agent', 'city': city.boss_code, 'page': 1})}")
        nav_search, fnav = sent[mark:], failed[fmark:]
        report["goto_search"] = {
            "attempts": len(nav_search),
            "aborted": len(fnav),
            "network_requests": len(nav_search) - len(fnav),
            "by_type_attempted": dict(Counter(t for t, _ in nav_search).most_common()),
        }
        print(f"① 导航到搜索页: 发起 {len(nav_search)} 次，被拦 {len(fnav)} 次 "
              f"→ 真实上网 {len(nav_search) - len(fnav)} 次")
        print(f"   {report['goto_search']['by_type_attempted']}\n", flush=True)

        # ── 打列表接口直到拿到 code=37 的 seed/name/ts ──
        # 不用等自然撞墙：直接连打列表，窗口 5-6 条就会给。
        print("② 连打列表接口，等 code=37 给出 seed/name/ts", flush=True)
        seed = name = ts = ""
        for i in range(1, 10):
            mark = len(sent)
            params = {"scene": "1", "query": "AI Agent", "city": city.boss_code,
                      "page": i, "pageSize": 30}
            result = await c._evaluate_fetch(f"{ORIGIN}{API_PATH}?{urlencode(params)}")
            raw = result.get("text") or ""
            cost = len(sent) - mark
            if "环境存在异常" in raw:
                body = json.loads(raw or "{}")
                zp = body.get("zpData") or {}
                seed, name, ts = (str(zp.get("seed") or ""),
                                  str(zp.get("name") or ""),
                                  str(zp.get("ts") or ""))
                print(f"   第 {i} 次命中 code=37（该次 fetch 发了 {cost} 次请求）")
                print(f"   seed={seed[:24]}... name={name} ts={ts}", flush=True)
                break
            print(f"   第 {i} 次正常（{cost} 次请求）", flush=True)
            await asyncio.sleep(3.0)

        if not (seed and name and ts):
            print("   9 次内没撞到 code=37，无法量重算成本。")
            report["error"] = "no code=37 within 9 list calls"
        else:
            # ── 量一次真实重算：两次导航 ──
            print("\n③ 触发一次 _recover_code37()，数它真实发了多少请求")
            mark, fmark = len(sent), len(failed)
            await c._recover_code37(seed, name, ts)
            seg, fseg = sent[mark:], failed[fmark:]
            net = len(seg) - len(fseg)     # 真上网的次数
            report["recover"] = {
                "attempts": len(seg),
                "aborted": len(fseg),
                "network_requests": net,
                "by_type_attempted": dict(Counter(t for t, _ in seg).most_common()),
                "by_host_network": dict(Counter(
                    h for t, h in seg).most_common()),
            }
            print(f"   发起 {len(seg)} 次，其中被拦 {len(fseg)} 次 "
                  f"→ **真实上网 {net} 次**")
            print(f"   发起构成 {report['recover']['by_type_attempted']}", flush=True)

            # ── 确认重算后 fetch 真的恢复 ──
            mark = len(sent)
            params = {"scene": "1", "query": "AI Agent", "city": city.boss_code,
                      "page": 1, "pageSize": 30}
            res = await c._evaluate_fetch(f"{ORIGIN}{API_PATH}?{urlencode(params)}")
            ok = "环境存在异常" not in (res.get("text") or "")
            report["after_recover_ok"] = ok
            print(f"\n④ 重算后 fetch 恢复: {ok}（{len(sent)-mark} 次请求）")
    finally:
        await c.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n" + "=" * 66)
    if "recover" in report:
        r = report["recover"]
        print(f"【采集侧重算成本】发起 {r['attempts']} 次，被拦 {r['aborted']} 次，"
              f"**真实上网 {r['network_requests']} 次**")
        print(f"  账本按 REQ_PER_RECOVER 记；对照刷新侧重算约 3 次。")
        print(f"  ⚠️ 只看『发起数』会把拦截当成没生效 —— abort 掉的请求"
              f"同样触发 page.on('request')。")
    print(f"结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
