"""列表扩充：用筛选跑多关键词多页列表接口，扩充岗位池并刷新存活。

只抓列表接口，不抓详情。把符合目标画像的岗位 upsert 进库，
同时刷新 last_seen_at 确认存活。

用法: python scripts/expand_list.py [--pages N] [--delay S]
  --pages  每个关键词抓多少页。**默认 0 = 按配置分词决定**
           （`pages_per_keyword` 里配了的用它，其余用 `pages_per_query`）；
           给了数值则对所有词统一覆盖。
  --delay  每页间隔秒数（默认 8）
  --dry-stop  连续几页零新增就整轮停止（默认 5，**跨关键词累计**）

## 2026-09-13 重写：把这一环收回共享通道（封禁事故后的收敛）

旧实现在这里自己拼 `page.evaluate(fetch)`，是三个采集脚本里唯一绕过
`collector` 的：于是它既不受 `max_requests_per_run`（单轮硬顶）约束，
也不产生调度器唯一识别的 `[RISK-STOP]` 标记。2026-09-12 的封禁就发生在
这个脚本里，而它的收尾是照常打印"列表扩充完成"、退出码 0。

现在的分工（每一条都对应那次事故里的一个具体形状）：

1. **请求只走 `BossCollector._same_origin_fetch`**：严格日线预留
   （`JobStore.reserve_request`，跨进程原子）、风控判定、持久熔断都在那条路上。
2. **风控 = 整轮退出 2**，并打 `[RISK-STOP]`。旧代码命中"账户存在异常"
   只 break 内层页循环，外层继续换下一个关键词（事故样本里 PyTorch 恰好是
   最后一个词才"看起来正常结束"）。不换词、不重试、不自动恢复 code=37。
3. **只有真翻到底才标 `exhausted`**：空 `jobList` 才是到底；风控、非 JSON、
   业务错误一律抬出整轮，绝不能被写成"这个词没货了"（那会让它被永久跳过）。
4. **连续零新增的计数器跨关键词累计**：旧实现每换一个词就重置，
   而 `--pages 4` 时单词语最多只有 4 页 —— `--dry-stop 5` **永远打不着**，
   于是整轮可以一路翻到第 43 页零新增还在继续。
5. **上限来自配置**：本轮关键词数 ≤ `max_keywords_per_run`、
   页号 ≤ `max_page_number`（**绝对上限**，没有命令行旁路 —— 要翻更深就改配置，
   让"翻多深"始终是一条写下来的策略，而不是一次随手加的开关）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.collectors.boss import (  # noqa: E402
    API_PATH,
    ORIGIN,
    BossCollector,
    RiskControlStop,
)
from job_radar.config import load_config  # noqa: E402
from job_radar.safety import AccessBlocked, AccessGate  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402
from job_radar.throttle import HumanThrottle, gaussian  # noqa: E402

# BUDGET 类停止：额度到顶是正常预算行为，不需要人工介入（与风控区分开，
# 否则巡检会把每一次"今天跑满了"误报成封禁信号）。
_BUDGET_MARKERS = ("今日累计请求已达", "账本不可用")


def _keyword_budget(cfg, keywords: list[str]) -> list[str]:
    """本轮关键词数上限（配置给的策略，命令行切片不能绕过）。"""
    limit = max(1, int(getattr(cfg, "max_keywords_per_run", 3)))
    if len(keywords) <= limit:
        return keywords
    if cfg.budget_mode == "reference":
        print(f"[REFERENCE] 本轮关键词 {len(keywords)} 个超过参考值 {limit}；参考模式继续执行显式计划。")
        return keywords
    print(f"本轮关键词 {len(keywords)} 个 > 上限 {limit}"
          f"（max_keywords_per_run），只跑前 {limit} 个：{keywords[:limit]}")
    print(f"  其余留到下一轮：{keywords[limit:]}")
    return keywords[:limit]


async def _search_page(collector: BossCollector, city) -> None:
    """把页面导到该城市的搜索页。

    同源 fetch 虽然由 URL 参数决定取数，但**页面上下文与请求参数不一致本身就是
    可被识别的异常**（真人换城市必然刷页），所以换城市要真的导过去。
    """
    await collector._goto_search(
        f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={city.boss_code}")


async def _expand(collector: BossCollector, cfg, store: JobStore, args,
                  cities, keywords: list[str], filters, page_ceiling: int) -> int:
    """跑完本轮，返回退出码（0=正常/预算到顶，2=风控熔断）。"""
    throttle = HumanThrottle(cfg)
    seen_new = seen_total = 0
    # ⚠️ 跨关键词累计：旧实现每换一个词就归零，于是 span < dry_stop 时
    # 这个机制结构性地永远不触发（事故那一轮 --pages 4 / --dry-stop 5）。
    dry_pages = 0
    print(f"页号上限 {page_ceiling}（max_page_number），"
          f"连续 {args.dry_stop} 页零新增即整轮停止（跨关键词累计）")

    for ci, city in enumerate(cities):
        if ci > 0:
            await _search_page(collector, city)
            collector.last_lid = ""   # lid 属于上一个城市的搜索会话
        for kw in keywords:
            done_page, exhausted = store.get_keyword_progress(city.name, kw)
            if exhausted and not args.restart:
                print(f"[{city.name}|{kw}] 已确认翻到底（第 {done_page} 页），跳过")
                continue
            start_page = 1 if args.restart else done_page + 1
            span = args.pages or cfg.pages_for(kw)
            end_page = start_page + span - 1
            if end_page > page_ceiling:
                # **绝对上限，没有命令行旁路**：要往深翻就改配置里的
                # `max_page_number`（1-30），让"翻多深"始终是一个写下来的策略，
                # 而不是一次随手加的开关。封禁那一轮就是在第 27-43 页发生的。
                if cfg.budget_mode == "enforce":
                    if start_page > page_ceiling:
                        print(f"[{city.name}|{kw}] 起始页 {start_page} 超过本次明确上限 {page_ceiling}，跳过。")
                        continue
                    end_page = page_ceiling
                else:
                    print(f"[REFERENCE] [{city.name}|{kw}] 计划页号 {end_page} 超过参考值 {page_ceiling}；该值不是平台安全界限。")
            if start_page > 1 or span != cfg.pages_per_query:
                print(f"[{city.name}|{kw}] 翻 第 {start_page}-{end_page} 页（本词 {span} 页）")

            for p in range(start_page, end_page + 1):
                await throttle.delay_before_request("list")
                params = collector._params(city, kw, filters, p)
                url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
                # 共享入口：额度预留 + 风控判定 + 持久熔断都在里面。
                # 抛出即意味着"这一页不该再试"（风控/预算/账本不可用）。
                body = await collector._same_origin_fetch(url)
                code = body.get("code")
                data = body.get("zpData")
                if code != 0 or not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
                    raise RuntimeError("列表响应结构不完整；不将该关键词标记为已到底")
                jobs = data["jobList"]
                if not jobs:
                    # 空 jobList = 真的翻到底（不是"这几页没新货"）。只有这一种
                    # 情况可以标 exhausted；错误/风控响应在上一步就已经抛出。
                    print(f"[{kw}|页{p}] code={code} 无岗位，确认翻到底")
                    store.set_keyword_progress(city.name, kw, p - 1, exhausted=True)
                    break
                page_new = page_active = 0
                for rank, raw in enumerate(jobs, start=1):
                    job = collector._parse_api_job(raw, kw, rank)
                    # 全保留分层：无效岗位跳过；画像外岗位标 sample 入库作去重样本。
                    if not job.title or not job.job_url:
                        continue
                    job.retention_status = ("active" if collector._should_keep(job)
                                            else "sample")
                    if store.upsert(job) == "inserted":
                        seen_new += 1
                        page_new += 1
                        if job.retention_status == "active":
                            page_active += 1
                    seen_total += 1
                print(f"[{kw}|页{p}] {len(jobs)} 条 → 本页新增 {page_new}"
                      f"（画像内 {page_active}），累计 {seen_total}，新增 {seen_new}")
                # 每页都记进度，进程被杀也不丢（本轮已翻的页不用重翻）
                store.set_keyword_progress(city.name, kw, p, last_new=page_new)
                if page_new == 0:
                    dry_pages += 1
                    if dry_pages >= args.dry_stop:
                        if cfg.budget_mode == "reference":
                            if dry_pages == args.dry_stop:
                                print(f"[REFERENCE] 连续 {dry_pages} 页零新增，建议调整关键词或深度；仍按显式计划执行。")
                            continue
                        print(f"[{kw}|页{p}] 连续 {dry_pages} 页零新增，整轮停止"
                              f"（不是翻到底，进度已存，下轮从第 {p + 1} 页续翻）")
                        print(f"\n列表扩充完成: 共处理 {seen_total} 条，新增 {seen_new} 条")
                        return 0
                else:
                    dry_pages = 0
                if p < end_page:
                    await asyncio.sleep(gaussian(args.delay, args.delay + 4))

    print(f"\n列表扩充完成: 共处理 {seen_total} 条，新增 {seen_new} 条")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml",
                        help="配置文件（可指向试验配置）")
    parser.add_argument("--pages", type=int, default=0,
                        help="每个关键词翻多少页。0（默认）= 读配置的 "
                             "pages_per_keyword / pages_per_query 按词分别决定；"
                             "给了数值则对所有词统一覆盖")
    parser.add_argument("--delay", type=float, default=8.0)
    parser.add_argument("--start", type=int, default=0,
                        help="从第几个关键词开始（0-based，跳过已完成的词）")
    parser.add_argument("--end", type=int, default=None,
                        help="到第几个关键词结束（0-based，不含），默认全跑")
    parser.add_argument("--dry-stop", type=int, default=5,
                        help="连续几页零新增就整轮停止（默认 5）。**跨关键词累计** —— "
                             "旧实现按词重置，配 --pages 4 时永远打不着")
    parser.add_argument("--over-quota", choices=("stop", "slow"), default=None,
                        help="兼容参数。日线现在是**硬线**：slow 只影响节奏，"
                             "不会再换来额外额度（严格预留见 JobStore.reserve_request）")
    parser.add_argument("--window-position", default=None,
                        help="浏览器窗口落点 x,y（默认用配置值）。坐标必须为正。")
    parser.add_argument("--restart", action="store_true",
                        help="忽略断点进度，所有关键词从第 1 页重新翻")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # 命令行覆盖配置：策略由本次调用决定，配置只给默认值。
    if args.over_quota:
        cfg.over_quota_action = args.over_quota
        print(f"[策略] 越线行为 = {args.over_quota}（额度仍是硬线）")
    if args.window_position is not None:
        cfg.browser.window_position = args.window_position
    # 遍历**全部**城市（只取 cities[0] 会静默跑三倍时间抓同一批数据）。
    cities = cfg.cities
    filters = cfg.filters[0] if cfg.filters else None
    keywords = cfg.keywords[args.start:args.end]
    if not keywords:
        print(f"关键词切片为空: start={args.start} end={args.end}")
        return 0
    keywords = _keyword_budget(cfg, keywords)
    page_ceiling = max(1, min(30, int(getattr(cfg, "max_page_number", 10))))
    print(f"本轮城市({len(cities)}个): {[c.name for c in cities]}")
    print(f"本轮关键词({len(keywords)}个): {keywords}")

    collector = BossCollector(cfg)
    with JobStore(cfg.database_path) as store:
        collector.ledger = store
        collector.ledger_script = "expand_list"
        # 策略文件就在库目录下（与 account/refresh 两条链路共用同一个答案）
        collector.gate = AccessGate(cfg.database_path)
        print(f"今日账本：{store.request_counts()}，单日上限 {cfg.max_requests_per_day}")
        try:
            # 共享 start：先过访问策略闸门（network_enabled + access-policy.json），
            # 再起浏览器。冻结期连窗口都不会出现。
            await collector.start()
            return await _expand(collector, cfg, store, args, cities, keywords,
                                 filters, page_ceiling)
        except AccessBlocked as exc:
            print(f"[ACCESS-BLOCKED] {exc}")
            return 4
        except RiskControlStop as exc:
            msg = str(exc)
            if any(m in msg for m in _BUDGET_MARKERS):
                print(f"[BUDGET-STOP] {msg}")
                print("[BUDGET-STOP] 无需人工介入，明日额度重置后可续跑。")
                return 0
            # 风控：整轮退出 2。调度器/巡检按 exit 2 与 [RISK-STOP] 双双可判。
            print(f"[RISK-STOP] {msg}")
            print("[RISK-STOP] 已持久熔断（access-policy.json），需人工复核后再解除。")
            return 2
        except Exception as exc:  # noqa: BLE001
            # 不再"换下一个关键词继续"：非预期错误会把请求花在未知状态上，
            # 而事故的形态正是"出错后继续跑完剩下的词"。
            print(f"[ABORT] 非预期错误，整轮停止：{type(exc).__name__}: {exc}")
            return 1
        finally:
            # 风控恢复导航后 close 偶发 TargetClosedError，数据已入库，忽略即可
            try:
                await collector.close()
            except Exception as exc:  # noqa: BLE001
                print(f"(ctx.close 竞态，数据已入库): {str(exc)[:60]}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
