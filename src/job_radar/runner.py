from __future__ import annotations

import json

from rich.console import Console
from rich.progress import Progress

from .collectors.boss import BossCollector, RiskControlStop
from .collectors.stubs import PendingPlatformCollector
from .models import CollectionConfig, CollectionStats, Platform
from .safety import AccessBlocked
from .storage import JobStore


console = Console()


def make_collector(platform: Platform, config: CollectionConfig):
    if platform is Platform.BOSS:
        return BossCollector(config)
    return PendingPlatformCollector(platform.value)


async def _run_one_query(collector, store, config, stats, city, keyword, filters,
                         pages: int) -> None:
    async for job in collector.collect(
        city,
        keyword,
        filters,
        pages,
        config.detail_limit_per_query,
    ):
        stats.list_records += 1
        if job.job_description:
            stats.detail_success += 1
        elif job.raw.get("detail_error"):
            stats.detail_failed += 1
        status = store.upsert(job)
        if status == "inserted":
            stats.inserted += 1
        else:
            stats.updated += 1
        chat = job.raw.get("auto_chat_status")
        if chat:
            store.record_chat_action(job.canonical_key(), "start_chat", chat)
            stats.auto_chat_planned += 1
            if chat == "executed":
                stats.auto_chat_executed += 1


def _record_run(store: JobStore, config: CollectionConfig, stats: CollectionStats) -> None:
    """落一次运行记录。**所有退出路径都要走到**（见 run_collection 的最外层 finally）。

    没有它，"今天新增多少 / 为什么停"只能靠翻日志；而 record_run 的入参里
    带着 `stopped_reason`，正是判断"这轮是正常跑完还是被熔断掐断"的现场依据。
    """
    label = getattr(config, "label", "") or ",".join(
        c.name for c in getattr(config, "cities", []) or []) or "runner"
    try:
        store.record_run(label, json.dumps(stats.model_dump(), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 记账失败不能盖掉真正的退出原因
        console.print(f"[yellow]record_run 写入失败: {type(exc).__name__}: {exc}[/]")


async def run_collection(config: CollectionConfig) -> CollectionStats:
    stats = CollectionStats()
    with JobStore(config.database_path) as store:
        try:
            for platform in config.platforms:
                collector = make_collector(platform, config)
                # 去重前置：把库里已有 JD 的岗位灌给采集器，详情配额不浪费在重复岗位上
                if hasattr(collector, "skip_detail_ids"):
                    collector.skip_detail_ids = store.detailed_ids()
                    console.print(f"[dim]已有详情 {len(collector.skip_detail_ids)} 条，本轮跳过其详情请求[/]")
                # 注入请求账本：单日累计上限跨进程生效（封禁按累计量算，不是频率）
                if hasattr(collector, "ledger"):
                    collector.ledger = store
                    collector.ledger_script = "runner"
                    console.print(f"[dim]今日账本 {store.request_counts()}"
                                  f"，单日上限 {config.max_requests_per_day}[/]")
                try:
                    # ⚠️ `start()` 必须在 try 内。它在**起浏览器之前**会先过访问策略
                    # 闸门（network_enabled + access-policy.json），这一步失败是常见
                    # 路径（冻结期、忘记解除、配置里没开）。原先 start 在 try 外，
                    # 一失败就既不 close 也不落 record_run —— 现场只留一个 traceback。
                    await collector.start()
                    keywords = config.keywords
                    if len(keywords) > config.max_keywords_per_run:
                        if config.budget_mode == "enforce":
                            keywords = keywords[:config.max_keywords_per_run]
                        else:
                            console.print("[yellow]关键词数量超过经验参考值；按本次显式配置执行。[/]")
                    for city in config.cities:
                        for keyword in keywords:
                            # 翻页深度按关键词取：`pages_per_keyword` 里配过的用它，
                            # 其余用 pages_per_query。原先一律传 pages_per_query，
                            # 于是配置里按词配的深度被静默忽略（高产词该深翻却只翻 3 页）。
                            pages = config.pages_for(keyword)
                            if pages > config.max_page_number and config.budget_mode == "enforce":
                                pages = config.max_page_number
                            for filters in config.filters:
                                stats.queries += 1
                                console.print(
                                    f"[bold cyan]{platform.value}[/] | {city.name} | {keyword} | {filters.model_dump(exclude_none=True)}"
                                )
                                try:
                                    await _run_one_query(
                                        collector, store, config, stats, city,
                                        keyword, filters, pages
                                    )
                                except NotImplementedError as exc:
                                    console.print(f"[yellow]{exc}[/]")
                                except AccessBlocked as exc:
                                    # 策略闸门：冻结/未启用。整轮停止，不再换城市或关键词。
                                    stats.stopped_reason = str(exc)
                                    console.print(f"[bold red]访问被策略拦住: {exc}[/]")
                                    return stats
                                except RiskControlStop as exc:
                                    # 命中风控或达到请求预算：整轮停止，不再换城市或关键词继续试探。
                                    stats.stopped_reason = str(exc)
                                    console.print(f"[bold red]已停止采集: {exc}[/]")
                                    return stats
                                except Exception as exc:
                                    console.print(f"[red]任务失败: {exc}[/]")
                except (AccessBlocked, RiskControlStop) as exc:
                    stats.stopped_reason = str(exc)
                    console.print(f"[bold red]本轮已停止: {exc}[/]")
                    return stats
                except Exception as exc:
                    stats.stopped_reason = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    # finally 在 return 之前执行，风控提前返回时这里同样会记上用量，
                    # 并保证浏览器被关掉（否则孤儿浏览器会自己重试，且不进任何账本）。
                    stats.requests_used += getattr(collector, "request_count", 0)
                    skipped = getattr(collector, "detail_skipped", 0)
                    if skipped:
                        console.print(f"[dim]详情去重跳过 {skipped} 次（省下同等数量的详情请求）[/]")
                    await collector.close()
        finally:
            # 这一层是"所有退出路径"的兜底：风控/策略 early-return、平台异常、
            # Ctrl+C 之外的任何抛出，record_run 都会被执行一次。
            _record_run(store, config, stats)
    return stats
