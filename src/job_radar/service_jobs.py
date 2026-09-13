"""Shared collection, enrichment, refresh and tagging operations."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .annotations import annotation_input, ensure_schema, has_annotation, last_status_times, record_status, save_annotation
from .candidate import passes_list_facts
from .models import JobRecord, Platform
from .safety import AccessGate
from .storage import JobStore
from .pacing import RequestPacer


FETCH_STATUS = r"""async (url) => {
 const r = await fetch(url, {credentials:'include'});
 const html = await r.text();
 const doc = new DOMParser().parseFromString(html,'text/html');
 const text = s => (doc.querySelector(s)?.textContent || '').trim();
 const h1 = text('.info-primary .name h1') || text('.job-banner h1');
 return {status:r.status,url:r.url,h1,job_status:text('.job-banner .job-status'),
 active_time:text('.boss-active-time'),title:text('title'),
 challenged:/\/(?:web\/user|safe\/verify)/.test(new URL(r.url).pathname) || (!h1 &&
   (/security-check|security.html|请完成验证|安全验证|注册登录/.test(html) || !!doc.querySelector('.verify-wrap,.captcha,.slider-verify'))),
 gone:r.status===404 && /页面不存在|404 Not Found/.test(html)};
}"""


def absolute(path: str, base: Path) -> str:
    value = Path(path)
    return str((value if value.is_absolute() else base/value).resolve())


def row_to_job(row) -> JobRecord:
    value = dict(row)
    for key in ("skills", "labels", "welfare", "matched_queries"):
        value[key] = json.loads(value.pop(key+"_json", "[]") or "[]")
    value["raw"] = json.loads(value.pop("raw_json", "{}") or "{}")
    return JobRecord.model_validate({k: v for k, v in value.items() if k in JobRecord.model_fields})


def configured(ctx, cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.database_path = str(ctx.database)
    cfg.browser.user_data_dir = absolute(cfg.browser.user_data_dir, ctx.settings.path.parent)
    cfg.auto_chat.enabled = False
    cfg.auto_chat.dry_run = True
    return cfg


def selected_rows(ctx, store, kind, cfg):
    where = "platform='boss'"
    if kind == "details":
        where += " AND trim(job_description)=''"
    elif kind == "tag":
        where += " AND trim(job_description)<>''"
    times = last_status_times(store.conn) if kind == "refresh" else {}
    columns = "*" if kind == "details" else "canonical_id,source_job_id,title,company_name,city,job_url,experience,education,job_description,detail_fetched_at,salary_min_k,salary_max_k,salary_text"
    result = []
    for source in store.conn.execute(f"SELECT {columns} FROM jobs WHERE {where} ORDER BY canonical_id"):
        row = dict(source)
        if ctx.initial.service.candidate_filtering and ctx.initial.candidate and not passes_list_facts(row, ctx.initial.candidate):
            continue
        if kind == "tag" and has_annotation(store.conn, row, cfg):
            continue
        if kind == "refresh":
            stamp = times.get(row["canonical_id"])
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if 0 <= (datetime.now(timezone.utc)-when).total_seconds() < cfg.refresh.after_hours * 3600:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass
        result.append(row)
        if len(result) >= ctx.task["options"]["limit"]:
            break
    return result


async def execute(ctx):
    cfg = configured(ctx, ctx.config())
    kind = ctx.task["kind"]
    limit = ctx.task["options"]["limit"]
    if ctx.task["options"]["dry_run"]:
        result = {"dry_run": True, "kind": kind, "limit": limit,
                  "queries": len(cfg.cities)*len(cfg.keywords)*len(cfg.filters),
                  "route": cfg.refresh.route if kind == "refresh" else "configured"}
        if kind != "collect":
            result.pop("queries")
            if ctx.database.exists():
                with JobStore(ctx.database, read_only=True) as source:
                    result["source_records"] = source.conn.execute("SELECT COUNT(*) FROM jobs WHERE platform='boss'").fetchone()[0]
        ctx.log("计划预览完成", kind="plan", plan=result)
        return result
    if kind != "tag":
        ctx.require_access()
    with JobStore(ctx.database) as store:
        ensure_schema(store.conn)
        if kind == "collect":
            return await collect(ctx, store, cfg)
        rows = selected_rows(ctx, store, kind, cfg)
        ctx.report(total=len(rows))
        if not rows:
            ctx.log("当前没有符合任务条件的记录")
            return dict(ctx.progress)
        if kind == "tag":
            return await tag(ctx, store, rows)
        if kind == "details":
            return await details(ctx, store, rows, cfg)
        return await refresh(ctx, store, rows, cfg)


async def collect(ctx, store, cfg):
    from .collectors.boss import BossCollector
    if cfg.platforms != [Platform.BOSS]:
        raise ValueError("当前任务支持 BOSS 平台，请选择已实现的平台")
    collector = BossCollector(cfg)
    pacer = RequestPacer(ctx.log)
    collector.pace = lambda kind: pacer.before(kind, collector.config)
    collector.online_check = lambda: ctx.settings.read()[0].network_enabled
    collector.ledger = store
    collector.ledger_script = "service.collect"
    collector.gate_source = f"managed-task:{ctx.ident}"
    collector.skip_detail_ids = store.detailed_ids()
    keywords = cfg.keywords[:cfg.max_keywords_per_run] if cfg.budget_mode == "enforce" else cfg.keywords
    plan = [(city, word, filters) for city in cfg.cities for word in keywords for filters in cfg.filters]
    ctx.report(total=ctx.task["options"]["limit"], queries_total=len(plan), queries_done=0)
    inserted = updated = 0
    try:
        await collector.start()
        for city, word, filters in plan:
            collector.config = configured(ctx, ctx.config())
            ctx.require_access()
            ctx.log(f"采集 {city.name} / {word}")
            pages = cfg.pages_for(word)
            if cfg.budget_mode == "enforce":
                pages = min(pages, cfg.max_page_number)

            async def query():
                nonlocal inserted, updated
                async for job in collector.collect(city, word, filters, pages, cfg.detail_limit_per_query):
                    if store.upsert(job) == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                    if job.raw.get("detail_error"):
                        ctx.progress["failed"] += 1
                    ctx.report(processed=ctx.progress["processed"]+1, inserted=inserted, updated=updated)
                    if ctx.progress["processed"] >= ctx.task["options"]["limit"]:
                        break

            await ctx.verified(query, collector.browser_session, collector.page)
            ctx.report(queries_done=ctx.progress["queries_done"]+1)
            if ctx.progress["processed"] >= ctx.task["options"]["limit"]:
                break
        return dict(ctx.progress)
    finally:
        await collector.close()


async def details(ctx, store, rows, cfg):
    from .collectors.boss import BossCollector
    collector = BossCollector(cfg)
    pacer = RequestPacer(ctx.log)
    collector.pace = lambda kind: pacer.before(kind, collector.config)
    collector.online_check = lambda: ctx.settings.read()[0].network_enabled
    collector.ledger = store
    collector.ledger_script = "service.details"
    collector.gate_source = f"managed-task:{ctx.ident}"
    try:
        await collector.start()
        for offset, row in enumerate(rows):
            if offset % cfg.service.batch_size == 0:
                cfg = configured(ctx, ctx.config())
                collector.config = cfg
            ctx.require_access()
            job = row_to_job(row)
            if not job.raw.get("securityId"):
                ctx.log(f"{row['canonical_id']} 缺少详情凭据，待重新采集列表", level="warning")
                ctx.progress["failed"] += 1
            else:
                async def operation():
                    result = await collector._collect_detail(job)
                    if not result.job_description.strip():
                        raise ValueError("详情接口未提供正文")
                    store.upsert(result)
                await ctx.verified(operation, collector.browser_session, collector.page)
            ctx.report(processed=offset+1)
        return dict(ctx.progress)
    finally:
        await collector.close()


async def tag(ctx, store, rows):
    from .llm import LLMError, extract_tags
    cfg = ctx.config()
    failures = 0
    for offset, row in enumerate(rows):
        if offset % cfg.service.batch_size == 0:
            cfg = ctx.config()
        if not cfg.llm.enabled:
            raise RuntimeError("LLM 已在配置中停用")
        fresh = store.conn.execute("SELECT * FROM jobs WHERE canonical_id=?", (row["canonical_id"],)).fetchone()
        if fresh is None:
            ctx.report(processed=offset+1)
            continue
        row = dict(fresh)
        if has_annotation(store.conn, row, cfg):
            ctx.log(f"{row['canonical_id']} 已有当前版本标签，跳过")
            ctx.report(processed=offset+1)
            continue
        try:
            payload, usage = await asyncio.to_thread(extract_tags, cfg.llm, annotation_input(row, cfg))
            save_annotation(store.conn, row, cfg, ctx.revision, ctx.ident, payload, usage)
            failures = 0
            ctx.log(f"已保存 {row['canonical_id']} 的标签", kind="annotation", model=cfg.llm.model)
        except LLMError as exc:
            failures += 1
            ctx.progress["failed"] += 1
            ctx.report(processed=offset+1)
            ctx.log(f"{row['canonical_id']}：{exc}", level="error")
            if failures >= 3:
                raise RuntimeError("连续三个标注请求失败，请检查 LLM 配置") from exc
        ctx.report(processed=offset+1)
    return dict(ctx.progress)


def status_verdict(row, evidence):
    from .collectors.boss import PlatformRiskStop
    from .refresh_fetch import is_closed_status, same_job
    if evidence.get("status") in (401, 403, 429) or evidence.get("challenged"):
        raise PlatformRiskStop("岗位状态请求需要平台验证，请在任务浏览器中处理")
    requested = urlsplit(row["job_url"])
    actual = urlsplit(evidence.get("url", ""))
    if (requested.hostname, requested.path) != (actual.hostname, actual.path):
        return None
    if evidence.get("gone") and evidence.get("status") == 404:
        return True
    if evidence.get("status") != 200 or not same_job(evidence.get("h1"), row["title"]):
        return None
    return is_closed_status(evidence.get("job_status"))


async def refresh(ctx, store, rows, cfg):
    from .collectors.boss import PlatformRiskStop
    from .analysis.decisions import safe_job_url
    from .collectors.browser import BrowserSession
    browser_cfg = cfg.browser.model_copy(deep=True)
    browser_cfg.mode = "persistent"
    browser_cfg.user_data_dir = absolute(cfg.refresh.profile_dir, ctx.settings.path.parent)
    browser_cfg.proxy_server = cfg.refresh.proxy_server if cfg.refresh.route == "proxy" else ""
    browser_cfg.proxy_username = cfg.refresh.proxy_username if cfg.refresh.route == "proxy" else ""
    browser_cfg.proxy_password = cfg.refresh.proxy_password if cfg.refresh.route == "proxy" else ""
    browser = BrowserSession(browser_cfg)
    pacer = RequestPacer(ctx.log)
    request_count = 0
    unresolved = 0
    try:
        ctx.require_access()
        context = await browser.start()
        page = await context.new_page()
        # Establish a same-origin page once. Platform verification remains interactive.
        first = next((r for r in rows if safe_job_url(r["job_url"])), None)
        if first is None:
            raise ValueError("没有有效的 BOSS 岗位链接")
        ctx.require_access()
        await pacer.before("list", cfg)
        token = store.reserve_request("detail_page", cfg.max_requests_per_day if cfg.budget_mode == "enforce" else None, script="service.refresh")
        try:
            await page.goto(first["job_url"], wait_until="domcontentloaded", timeout=browser_cfg.navigation_timeout_ms)
            finish_request(store, token, "ok")
        except Exception:
            finish_request(store, token, "error")
            raise
        request_count += 1
        for offset, row in enumerate(rows):
            if offset % cfg.service.batch_size == 0:
                cfg = configured(ctx, ctx.config())
            url = safe_job_url(row["job_url"])
            if not url:
                ctx.progress["failed"] += 1
                ctx.report(processed=offset+1)
                continue

            async def operation():
                nonlocal request_count, unresolved
                ctx.require_access()
                await pacer.before("list", cfg)
                ctx.require_access()
                if cfg.budget_mode == "enforce" and request_count >= cfg.max_requests_per_run:
                    raise RuntimeError("本轮请求已达到配置上限")
                token = store.reserve_request("detail_page", cfg.max_requests_per_day if cfg.budget_mode == "enforce" else None, script="service.refresh")
                request_count += 1
                if cfg.budget_mode == "reference":
                    used = store.request_counts().get("detail_page", 0)
                    if request_count == cfg.max_requests_per_run+1 or used == cfg.max_requests_per_day+1:
                        ctx.log("已超过配置中的请求参考线", level="warning", kind="budget", requests=request_count, today=used)
                try:
                    evidence = await page.evaluate(FETCH_STATUS, url)
                    verdict = status_verdict(row, evidence)
                    finish_request(store, token, "ok" if verdict is not None else "unknown")
                except PlatformRiskStop:
                    finish_request(store, token, "risk")
                    raise
                except Exception:
                    finish_request(store, token, "error")
                    raise
                record_status(store.conn, row, ctx.ident, evidence, verdict)
                if verdict is None:
                    ctx.progress["failed"] += 1
                    unresolved += 1
                else:
                    unresolved = 0
                ctx.log(f"{row['canonical_id']}：" + ("待核实" if verdict is None else "已关闭" if verdict else "招聘中"))
                if unresolved >= 3:
                    ctx.report(processed=offset+1, requests=request_count)
                    raise RuntimeError("连续三条状态无法判读，本轮已停止，请检查页面与解析结果")

            await ctx.verified(operation, browser, page)
            ctx.report(processed=offset+1, requests=request_count)
        return dict(ctx.progress)
    finally:
        await browser.close()


def finish_request(store, token, outcome):
    if not store.mark_outcome(token, outcome):
        raise RuntimeError("请求账本回填失败，任务停止")
