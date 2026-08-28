from __future__ import annotations

from rich.console import Console
from rich.progress import Progress

from .collectors.boss import BossCollector, RiskControlStop
from .collectors.stubs import PendingPlatformCollector
from .models import CollectionConfig, CollectionStats, Platform
from .storage import JobStore


console = Console()


def make_collector(platform: Platform, config: CollectionConfig):
    if platform is Platform.BOSS:
        return BossCollector(config)
    return PendingPlatformCollector(platform.value)


async def _run_one_query(collector, store, config, stats, city, keyword, filters) -> None:
    async for job in collector.collect(
        city,
        keyword,
        filters,
        config.pages_per_query,
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


async def run_collection(config: CollectionConfig) -> CollectionStats:
    stats = CollectionStats()
    with JobStore(config.database_path) as store:
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
            await collector.start()
            try:
                for city in config.cities:
                    for keyword in config.keywords:
                        for filters in config.filters:
                            stats.queries += 1
                            console.print(
                                f"[bold cyan]{platform.value}[/] | {city.name} | {keyword} | {filters.model_dump(exclude_none=True)}"
                            )
                            try:
                                await _run_one_query(
                                    collector, store, config, stats, city, keyword, filters
                                )
                            except NotImplementedError as exc:
                                console.print(f"[yellow]{exc}[/]")
                            except RiskControlStop as exc:
                                # 命中风控或达到请求预算：整轮停止，不再换城市或关键词继续试探。
                                stats.stopped_reason = str(exc)
                                console.print(f"[bold red]已停止采集: {exc}[/]")
                                return stats
                            except Exception as exc:
                                console.print(f"[red]任务失败: {exc}[/]")
            finally:
                # finally 在 return 之前执行，风控提前返回时这里同样会记上用量。
                stats.requests_used += getattr(collector, "request_count", 0)
                skipped = getattr(collector, "detail_skipped", 0)
                if skipped:
                    console.print(f"[dim]详情去重跳过 {skipped} 次（省下同等数量的详情请求）[/]")
                await collector.close()
    return stats
