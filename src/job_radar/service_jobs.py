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
from .rounds import CollectionRound, balanced_rows, encode, query_plan


FETCH_STATUS = r"""async ({url,timeout}) => {
 const controller = new AbortController();
 const timer = setTimeout(() => controller.abort(), timeout);
 try {
 const r = await fetch(url, {credentials:'include', signal:controller.signal});
 const html = await r.text();
 const doc = new DOMParser().parseFromString(html,'text/html');
 const text = s => (doc.querySelector(s)?.textContent || '').trim();
 const h1 = text('.info-primary .name h1') || text('.job-banner h1');
 return {status:r.status,url:r.url,h1,job_status:text('.job-banner .job-status'),
 active_time:text('.boss-active-time'),title:text('title'),
 captcha_present:!!doc.querySelector('.verify-wrap,.captcha,.slider-verify,#nc_1_n1z'),
 restriction_notice:!h1 && /账户存在异常|IP地址存在异常|IP 地址存在异常|访问频繁|操作过于频繁|操作太频繁/.test(html),
 challenged:/\/(?:web\/user|safe\/verify)/.test(new URL(r.url).pathname) || (!h1 &&
   (/security-check|security.html|请完成验证|安全验证|注册登录/.test(html) || !!doc.querySelector('.verify-wrap,.captcha,.slider-verify'))),
 gone:r.status===404 && /页面不存在|404 Not Found/.test(html)};
 } finally { clearTimeout(timer); }
}"""


READ_LOADED_STATUS = r"""() => {
 const text = s => (document.querySelector(s)?.innerText || '').trim();
 const visible = e => e.getClientRects().length > 0;
 const h1 = text('.info-primary .name h1') || text('.job-banner h1');
 const captcha = [...document.querySelectorAll('.verify-wrap,.captcha,.slider-verify,#nc_1_n1z')].some(visible);
 return {url:location.href,h1,job_status:text('.job-banner .job-status'),
 active_time:text('.boss-active-time'),title:document.title,
 challenged:captcha || /\/(?:web\/user|safe\/verify|web\/passport\/zp\/security\.html)/.test(location.pathname),
 captcha_present:captcha,gone:false,source:'ordinary_page_navigation'};
}"""


def page_session_check(evidence):
    """Recognize only the observed JS-session document, never explicit CAPTCHA or restriction."""
    location = urlsplit(evidence.get('url') or '')
    return (evidence.get('status') == 200 and location.scheme == 'https'
            and location.netloc == 'www.zhipin.com'
            and location.path == '/web/passport/zp/security.html'
            and evidence.get('captcha_present') is False
            and evidence.get('restriction_notice') is False)


def absolute(path: str, base: Path) -> str:
    value = Path(path)
    return str((value if value.is_absolute() else base/value).resolve())


def row_to_job(row) -> JobRecord:
    from .models import job_from_row

    return job_from_row(row)


def configured(ctx, cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.database_path = str(ctx.database)
    cfg.browser.user_data_dir = absolute(cfg.browser.user_data_dir, ctx.settings.path.parent)
    cfg.auto_chat.enabled = False
    cfg.auto_chat.dry_run = True
    return cfg


def selection_where(ctx, store, kind, cfg):
    where = "platform='boss'"
    round_id = ctx.task["options"].get("round_id", "")
    if round_id:
        if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='collection_rounds'").fetchone() or not store.conn.execute(
            "SELECT 1 FROM collection_rounds WHERE round_id=?", (round_id,)).fetchone():
            raise ValueError(f"采集批次不存在：{round_id}")
        where += " AND canonical_id IN (SELECT canonical_id FROM collection_observations WHERE round_id=?)"
    params = (round_id,) if round_id else ()
    canonical_ids = list(dict.fromkeys(ctx.task["options"].get("canonical_ids", [])))
    if canonical_ids:
        where += " AND canonical_id IN (" + ",".join("?" for _ in canonical_ids) + ")"
        params += tuple(canonical_ids)
    if kind == "details":
        where += " AND (trim(job_description)='' OR json_extract(raw_json,'$.detail_access_verified') IS NOT 1 OR julianday(detail_fetched_at) IS NULL OR julianday(detail_fetched_at) > julianday('now') OR julianday(detail_fetched_at) < julianday('now') - ? / 24.0)"
        params += (cfg.service.detail_after_hours,)
    elif kind == "tag":
        where += " AND trim(job_description)<>'' AND json_extract(raw_json,'$.detail_access_verified') IS NOT 0"
        if round_id:
            where += " AND json_extract(raw_json,'$.detail_access_verified')=1"
            if cfg.service.detail_after_hours:
                where += " AND julianday(detail_fetched_at) BETWEEN julianday('now') - ? / 24.0 AND julianday('now')"
                params += (cfg.service.detail_after_hours,)
            else:
                where += " AND julianday(detail_fetched_at) BETWEEN (SELECT julianday(created_at) FROM collection_rounds WHERE round_id=?) AND julianday('now')"
                params += (round_id,)
    return where, params


def selected_rows(ctx, store, kind, cfg):
    where, params = selection_where(ctx, store, kind, cfg)
    round_id = ctx.task["options"].get("round_id", "")
    times = last_status_times(store.conn) if kind == "refresh" else {}
    columns = "*" if kind == "details" else "canonical_id,source_job_id,title,company_name,city,job_url,experience,education,job_description,detail_fetched_at,salary_min_k,salary_max_k,salary_text"
    result = []
    for source in store.conn.execute(f"SELECT {columns} FROM jobs WHERE {where} ORDER BY last_seen_at DESC, canonical_id", params):
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
        if not round_id and len(result) >= ctx.task["options"]["limit"]:
            break
    return balanced_rows(store.conn, round_id, result, ctx.task["options"]["limit"]) if round_id else result


async def execute(ctx):
    cfg = configured(ctx, ctx.config())
    kind = ctx.task["kind"]
    limit = ctx.task["options"]["limit"]
    if ctx.task["options"]["dry_run"]:
        result = {"dry_run": True, "kind": kind, "limit": limit,
                  "queries": len(cfg.cities)*len(cfg.keywords)*len(cfg.filters),
                  "route": cfg.refresh.route if kind == "refresh" else "configured"}
        if kind == "recommend":
            result["plan"] = {"source_kind": "recommendation", "pages": ctx.task["options"].get("recommend_pages", 2),
                              "expectation_label": ctx.task["options"].get("expectation_label", "")}
            result["round_id"] = ctx.task["options"].get("round_id", "") or ctx.ident
            result.pop("queries")
        elif kind == "collect":
            result["plan"] = query_plan(cfg)
            result["round_id"] = ctx.task["options"].get("round_id", "") or ctx.ident
            result["queries"] = len(result["plan"])
        else:
            result.pop("queries")
            if ctx.database.exists():
                with JobStore(ctx.database, read_only=True) as source:
                    result["source_records"] = source.conn.execute("SELECT COUNT(*) FROM jobs WHERE platform='boss'").fetchone()[0]
                    # Preview uses the exact selection, including cohort and freshness.
                    result["selected_records"] = len(selected_rows(ctx, source, kind, cfg))
        ctx.log("计划预览完成", kind="plan", plan=result)
        return result
    if kind != "tag":
        ctx.require_access()
    with JobStore(ctx.database) as store:
        ensure_schema(store.conn)
        if kind == "collect":
            return await collect(ctx, store, cfg)
        if kind == "recommend":
            return await recommend(ctx, store, cfg)
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
    collector.skip_detail_ids = store.detailed_ids(cfg.service.detail_after_hours)
    from .models import CityConfig, SearchFilters
    rounds = CollectionRound(store)
    round_id = ctx.task["options"].get("round_id", "") or ctx.ident
    plan = rounds.begin(round_id, ctx.ident, cfg)
    completed = rounds.completed(round_id)
    ctx.report(total=ctx.task["options"]["limit"], queries_total=len(plan), queries_done=len(completed),
               round_id=round_id, round_jobs=rounds.count(round_id), duplicates=0, detail_success=0)
    ctx.log(f"采集批次 {round_id}：{len(plan)} 组查询，已完成 {len(completed)} 组", kind="round")
    inserted = updated = 0
    final_status = "partial"
    try:
        if len(completed) == len(plan):
            final_status = "complete"
            ctx.log("该批次查询已经完成；更新检索结果请使用新的 round_id")
            return dict(ctx.progress)
        await collector.start()
        for item in plan:
            if encode(item) in completed:
                continue
            city = CityConfig.model_validate(item["city"])
            word = item["keyword"]
            filters = SearchFilters.model_validate(item["filters"])
            collector.config = configured(ctx, ctx.config())
            ctx.require_access()
            ctx.log(f"采集 {city.name} / {word}")

            def save_list(job):
                nonlocal inserted, updated
                if store.upsert(job) == "inserted":
                    inserted += 1
                else:
                    updated += 1
                first_in_round = rounds.observe(round_id, job.canonical_key(), item)
                ctx.report(processed=ctx.progress["processed"] + int(first_in_round), inserted=inserted, updated=updated,
                           duplicates=ctx.progress["duplicates"] + int(not first_in_round), round_jobs=rounds.count(round_id))

            collector.on_list = save_list

            async def query():
                async for job in collector.collect(city, word, filters, item["pages"], cfg.detail_limit_per_query):
                    if job.job_description.strip():
                        store.upsert(job, observed=False)
                        rounds.update_detail(round_id, job.canonical_key())
                        ctx.progress["detail_success"] += 1
                    if job.raw.get("detail_error"):
                        ctx.progress["failed"] += 1
                        ctx.log(f"{job.canonical_key()} 详情失败：{job.raw['detail_error']}", level="warning")
                    ctx.report(requests=collector.request_count)
                    if ctx.progress["processed"] >= ctx.task["options"]["limit"]:
                        return False
                return True

            exhausted = await ctx.verified(query, collector.browser_session, collector.page)
            if exhausted:
                rounds.complete_query(round_id, item)
                completed.add(encode(item))
                ctx.report(queries_done=len(completed))
            else:
                break
        final_status = "complete" if len(completed) == len(plan) else "partial"
        if final_status == "complete":
            ctx.report(total=ctx.progress["processed"])
        ctx.report(round_status=final_status, requests=collector.request_count)
        return dict(ctx.progress)
    finally:
        rounds.finish(round_id, final_status)
        await collector.close()


async def recommend(ctx, store, cfg):
    from .collectors.recommendation import RecommendationCollector
    from .rounds import stamp
    cfg.list_source = "native"
    collector = RecommendationCollector(cfg)
    collector.ledger = store
    collector.ledger_script = "service.recommend"
    collector.gate_source = f"managed-task:{ctx.ident}"
    collector.online_check = lambda: ctx.settings.read()[0].network_enabled
    pacer = RequestPacer(ctx.log)
    collector.pace = lambda kind: pacer.before(kind, collector.config)
    rounds = CollectionRound(store)
    options = ctx.task["options"]
    ident = options.get("round_id") or ctx.ident
    plan = {"source_kind": "recommendation", "page_limit": options.get("recommend_pages", 2),
            "expectation_label": options.get("expectation_label", "")}
    existing = store.conn.execute("SELECT plan_json FROM collection_rounds WHERE round_id=?", (ident,)).fetchone()
    if existing and json.loads(existing[0]) != plan:
        raise ValueError("推荐批次的来源、页数或求职期望改变，请使用新round_id")
    if not existing:
        rounds.begin_import(ident, plan, {"mode": "live-recommendation", "sampling": plan}, stamp())
    with store.conn:
        store.conn.execute("UPDATE collection_rounds SET status='collecting',last_task_id=? WHERE round_id=?", (ctx.ident, ident))
    ctx.report(round_id=ident, total=options["limit"], duplicates=0, pages=0)
    outcome = "partial"
    seen = set()
    try:
        await collector.start()
        if plan["expectation_label"]:
            from .collectors.boss import SEARCH_BASE
            await collector._goto_search(SEARCH_BASE)
        for number in range(1, plan["page_limit"] + 1):
            collector.config = configured(ctx, ctx.config())
            collector.config.list_source = "native"
            ctx.require_access()
            body = await ctx.verified(lambda: collector.recommendation_page(number,
                expectation_label=plan["expectation_label"], first=number == 1), collector.browser_session, collector.page)
            raw_jobs = body["zpData"]["jobList"]
            page_ids = {r.get("encryptJobId") for r in raw_jobs}
            if number > 1 and raw_jobs and not page_ids - seen:
                raise RuntimeError("推荐下一页没有新岗位，停止当前批次以免重复请求")
            query = {"source_kind": "recommendation", "page": number,
                     "expectation_context_hash": body["_list_source"]["expectation_context_hash"]}
            for rank, raw in enumerate(raw_jobs):
                job = collector._parse_api_job(raw, "recommendation:" + (plan["expectation_label"] or "mixed"), (number - 1) * 15 + rank + 1)
                job.raw["list_source"] = {**body["_list_source"], "observed_at_utc": stamp()}
                store.upsert(job)
                added = rounds.observe(ident, job.canonical_key(), query)
                ctx.progress["processed"] += int(added)
                ctx.progress["duplicates"] += int(not added)
            seen.update(page_ids)
            rounds.complete_query(ident, query)
            ctx.report(pages=number, round_jobs=rounds.count(ident), requests=collector.request_count)
            ctx.log(f"推荐第{number}页：{len(raw_jobs)}条，批次{rounds.count(ident)}个主键", kind="recommendation")
            if body["zpData"].get("hasMore") is False or ctx.progress["processed"] >= options["limit"]:
                break
        outcome = "bounded_sample_complete"
        ctx.report(total=ctx.progress["processed"])
        return dict(ctx.progress)
    finally:
        rounds.finish(ident, outcome)
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
    round_id = ctx.task["options"].get("round_id", "")
    rounds = CollectionRound(store) if round_id else None
    consecutive_failures = 0
    try:
        await collector.start()
        for offset, row in enumerate(rows):
            cfg = configured(ctx, ctx.config())
            collector.config = cfg
            ctx.require_access()
            job = row_to_job(row)
            if not job.raw.get("securityId"):
                ctx.log(f"{row['canonical_id']} 缺少详情凭据，待重新采集列表", level="warning")
                ctx.progress["failed"] += 1
            else:
                async def operation():
                    collector.config = configured(ctx, ctx.config())
                    result = await collector._collect_detail(job)
                    if not result.job_description.strip():
                        raise ValueError("详情接口未提供正文")
                    store.upsert(result, observed=False)
                    if rounds:
                        rounds.update_detail(round_id, result.canonical_key())
                # Each planned detail batch uses the site's normal search page context.
                # Platform verification still pauses the task; errors never auto-release it.
                if offset % cfg.detail_batch == 0 or not collector.page.url.startswith("https://www.zhipin.com/"):
                    from .collectors.boss import SEARCH_BASE
                    await collector._goto_search(SEARCH_BASE)
                try:
                    await ctx.verified(operation, collector.browser_session, collector.page)
                    consecutive_failures = 0
                    ctx.log(f"已更新 {row['canonical_id']} 的正文", kind="detail")
                except (ValueError, RuntimeError) as exc:
                    from .collectors.boss import RiskControlStop
                    from .safety import AccessBlocked
                    if isinstance(exc, (RiskControlStop, AccessBlocked)):
                        raise
                    ctx.progress["failed"] += 1
                    consecutive_failures += 1
                    ctx.log(f"{row['canonical_id']} 详情失败：{exc}", level="warning")
                    if consecutive_failures >= 3:
                        raise RuntimeError("连续三条详情失败，任务停止；可重采该批次列表更新详情凭据") from exc
            ctx.report(processed=offset+1, requests=collector.request_count, round_id=round_id)
        return dict(ctx.progress)
    finally:
        await collector.close()


async def tag(ctx, store, rows):
    from .llm import LLMError, extract_tags
    offset = 0
    failures = 0
    while offset < len(rows):
        cfg = ctx.config()
        if not cfg.llm.enabled:
            raise RuntimeError("LLM 已在配置中停用")
        size = min(cfg.llm.concurrency, cfg.service.batch_size)
        batch = rows[offset:offset+size]
        pending = []
        eligible_where, eligible_params = selection_where(ctx, store, "tag", cfg)
        for selected in batch:
            fresh = store.conn.execute(
                f"SELECT * FROM jobs WHERE canonical_id=? AND {eligible_where}",
                (selected["canonical_id"], *eligible_params)).fetchone()
            if fresh is None:
                ctx.log(f"{selected['canonical_id']} 当前正文已不符合抽取条件，跳过")
                continue
            row = dict(fresh)
            if has_annotation(store.conn, row, cfg):
                ctx.log(f"{row['canonical_id']} 已有当前版本标签，跳过")
                continue
            pending.append(row)
        ctx.report(concurrency=size)
        # Network requests run concurrently; SQLite writes stay in the worker's main thread.
        results = await asyncio.gather(*[
            asyncio.to_thread(extract_tags, cfg.llm, annotation_input(row, cfg)) for row in pending
        ], return_exceptions=True)
        successes = 0
        batch_failures = 0
        for row, result in zip(pending, results):
            if isinstance(result, LLMError):
                ctx.progress["failed"] += 1
                batch_failures += 1
                ctx.log(f"{row['canonical_id']}：{result}", level="error")
            elif isinstance(result, BaseException):
                raise result
            else:
                payload, usage = result
                save_annotation(store.conn, row, cfg, ctx.revision, ctx.ident, payload, usage)
                successes += 1
                ctx.log(f"已保存 {row['canonical_id']} 的标签", kind="annotation", model=cfg.llm.model)
        offset += len(batch)
        ctx.report(processed=offset, annotations=ctx.progress.get("annotations", 0)+successes)
        failures = 0 if successes else failures + len(pending)
        if failures >= 3 or batch_failures >= 3:
            raise RuntimeError("一批或连续批次中至少三个标注请求失败；请检查 LLM 配置")
    return dict(ctx.progress)


def status_verdict(row, evidence):
    """三态判定：True 已关闭 / False 在招 / None 判不了。风控态抛 PlatformRiskStop。

    判定本身不在这里实现 —— 复用 `boss_scrape.refresh.classify`，与批量刷新同一套判据。
    这里只做任务链路特有的两件事：把风控态翻译成任务级异常（要人去浏览器过验证），
    以及**校验取回来的 URL 就是请求的那条**（重定向会让判定张冠李戴，而降级不可逆）。
    """
    from boss_scrape.refresh import classify
    from .collectors.boss import PlatformRiskStop

    status = classify(evidence, title=row["title"])
    if status.state == "challenged" or evidence.get("challenged") \
            or evidence.get("status") in (401, 403, 429):
        raise PlatformRiskStop("岗位状态请求需要平台验证，请在任务浏览器中处理",
                               http_status=evidence.get("status"),
                               request_url=row.get("job_url", ""))
    requested = urlsplit(row["job_url"])
    actual = urlsplit(evidence.get("url", ""))
    if (requested.hostname, requested.path) != (actual.hostname, actual.path):
        return None
    if status.state == "gone" or (evidence.get("gone") and evidence.get("status") == 404):
        return True
    if evidence.get("status") != 200:
        return None
    return status.closed


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
    if cfg.refresh.route == "proxy" and browser_cfg.user_data_dir == cfg.browser.user_data_dir:
        raise ValueError("跳板刷新须使用独立的匿名浏览器目录，不能复用采集登录会话")
    browser = BrowserSession(browser_cfg)
    pacer = RequestPacer(ctx.log)
    request_count = 0
    unresolved = 0
    page_recoveries = 0
    roller = None
    try:
        ctx.require_access()
        context = await browser.start()
        # 就地滚动 stoken。
        #
        # 2026-10-18 实测：岗位页 HTML 的响应**同样下发** `__zp_sseed__` /
        # `__zp_sname__` / `__zp_sts__` 三件套，所以能就地算出新 token，
        # 成本 0 个请求。而原先窗口耗尽（约 5.5 条）时走的是「普通页面导航」恢复，
        # 那一次会把整个 SPA 拉起来——实测几十到上百个请求，摊薄到每条是 +0.6。
        #
        # 滚动成功就不需要那条恢复路径；滚动失败（页面漂走、签名脚本取不到）
        # 才退回原来的导航恢复，行为不变。
        from .collectors.stoken import StokenRoller
        roller = StokenRoller(context, origin="https://www.zhipin.com")
        await roller.start()
        async def check_session():
            if cfg.refresh.route == "proxy":
                names = {cookie["name"] for cookie in await context.cookies(["https://www.zhipin.com/"])}
                if names & {"wt2", "wt", "geek_zp_token", "zp_at", "bst"}:
                    raise ValueError("跳板刷新会话含登录凭据，请使用匿名会话；验证窗口只完成验证，不登录账号")
        await check_session()
        ctx.log(f"刷新路线：{cfg.refresh.route}，会话：{cfg.refresh.profile_dir}", kind="route")
        page = await context.new_page()
        # Establish a same-origin page once. Platform verification remains interactive.
        first = next((r for r in rows if safe_job_url(r["job_url"])), None)
        if first is None:
            raise ValueError("没有有效的 BOSS 岗位链接")
        ctx.require_access()
        await pacer.before("list", cfg)
        token = store.log_request("detail_page", script="service.refresh")
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
                nonlocal request_count, unresolved, page_recoveries
                await check_session()
                ctx.require_access()
                await pacer.before("list", cfg)
                ctx.require_access()
                # 只记一行观测：请求量与节奏由 pacing/风控熔断负责，不由自设日线决定放行。
                token = store.log_request("detail_page", script="service.refresh")
                request_count += 1
                try:
                    # 取值与判定已抽到 boss_scrape.refresh：一次 fetch 一个文档请求，
                    # 取完就地滚 token，窗口不再耗尽，也就不再需要那条整页导航恢复。
                    from boss_scrape.refresh import refresh_one

                    async def account_recovery():
                        """自愈时那次导航也是真实请求：先记一行观测，再发。"""
                        nonlocal request_count
                        await check_session()
                        ctx.require_access()
                        await pacer.before("list", cfg)
                        store.log_request("detail_page", script="service.refresh.recovery")
                        request_count += 1
                        ctx.report(requests=request_count)

                    observed = await refresh_one(page, url, title=row["title"],
                                                 roller=roller,
                                                 before_recover=account_recovery)
                    evidence = observed.raw
                    if page_session_check(evidence) and page_recoveries < 3:
                        # Keep the failed read as evidence. One ordinary navigation lets
                        # the website update its own session; no credential/token synthesis.
                        finish_request(store, token, 'unknown')
                        record_status(store.conn, row, ctx.ident,
                            {**evidence, 'page_session_check': True, 'recovery': 'ordinary_page_navigation'}, None)
                        await pacer.before('list', cfg)
                        ctx.require_access()
                        await check_session()
                        token = store.log_request('detail_page', script='service.refresh.page-recovery')
                        request_count += 1
                        page_recoveries += 1
                        ctx.report(page_recoveries=page_recoveries)
                        ctx.log(f'页面会话需复检，普通加载当前岗位一次（{page_recoveries}/3）', kind='page-session')
                        response = await page.goto(url, wait_until='domcontentloaded', timeout=browser_cfg.navigation_timeout_ms)
                        await page.wait_for_timeout(1500)
                        await check_session()
                        evidence = await page.evaluate(READ_LOADED_STATUS)
                        evidence['status'] = response.status if response else None
                    verdict = status_verdict(row, evidence)
                    finish_request(store, token, "ok" if verdict is not None else "unknown")
                except PlatformRiskStop:
                    finish_request(store, token, "risk")
                    record_status(store.conn, row, ctx.ident, {**evidence, "access_issue": True}, None)
                    raise
                except Exception:
                    finish_request(store, token, "error")
                    raise
                record_status(store.conn, row, ctx.ident, evidence, verdict)
                round_id = ctx.task["options"].get("round_id", "")
                if round_id:
                    CollectionRound(store).update_detail(round_id, row["canonical_id"])
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
        try:
            if roller is not None:
                await roller.close()
        except Exception:  # noqa: BLE001 - 收尾失败不该掩盖主流程异常
            pass
        await browser.close()


def finish_request(store, token, outcome):
    if not store.mark_outcome(token, outcome):
        raise RuntimeError("请求账本回填失败，任务停止")
