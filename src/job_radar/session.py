"""可托管的采集会话：推荐流校准 → 关键词扫描 → 补存量正文。

`local/analysis/live-20261019/run_session.py` 是一次性脚本，只有本机 shell 能跑。
这里把同一条链路做成**包内的任务种类**（`kind="session"`），于是三种调用方式共用
同一个任务核心：

- LLM：`job-radar task start session --limit ...`（HTTP + JSON，见 `service_cli.py`）
- 前端：`POST /api/tasks` `{"kind": "session", "options": {...}}`
- 本机：`job-radar session --phase ...`（`run_task`，不需要先 `serve`）

## 热更新怎么生效

任务运行中改配置会**即时生效**，靠的是两件事同时成立：

1. 每次查询 / 每条详情之前都重新取一份配置（`fresh()` → `worker.TaskContext.config()`，
   `HOT_FIELDS` 里的字段会换成新值）；
2. 所有节奏点都接在 `RequestPacer(ctx.log)` 上，而 `collector.pace` 这个 lambda
   每次被调用时读的是 `collector.config` —— 所以下一跳就吃到新节奏。

`HOT_FIELDS` 里的 `request_delay_seconds` / `detail_delay_seconds` / `detail_batch` /
`detail_cooldown_seconds` / `burst_threshold` / `burst_penalty_seconds` 都在这条路上。

## 三个阶段（`options["phase"]`，默认 `calibrate`）

| phase | 做什么 | 产出 |
|---|---|---|
| `calibrate` | 推荐流采样 + 抓正文 | JSONL 校准记录 + 入库 |
| `sweep` | 关键词轮转扫描 + 就地补详情 | 入库 |
| `backfill` | 给 active 且缺正文的存量岗位补详情 | 入库 |

## 为什么校准必须两段式（踩过的坑，别退回去）

`collector._collect_ssr_detail()` 会 `page.goto` 到 `/job_detail/...`，**离开推荐流页面**。
如果边翻页边抓正文，下一页的 `window.scrollTo` 就滚在详情页上，等不到推荐响应：

    2026-10-19 实测 recommend_error page=2 error=TimeoutError: Timeout 30000ms exceeded

所以第一段只翻页（全程停在推荐流上），翻完再逐条抓正文。第二段已经把页面翻走了，
翻页也已经结束，两边不打架。

## 风控

不自己写重试。撞墙就停：`ctx.verified(...)` 负责把 `PlatformRiskStop` 翻译成
「任务暂停 + 等人工验证」，`AccessGate` 负责持久熔断，`ctx.require_access()` 负责
每次请求前复查策略。这里只做业务级容错（某条详情失败记 `failed`，连续三条就停），
与 `service_jobs.details()` 同一套判据。
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import CityConfig, SearchFilters
from .pacing import RequestPacer
from .rounds import rotate_keywords
from .service_jobs import absolute, configured, row_to_job, selection_where


#: 支持的阶段。顺序就是推荐的使用顺序：先校准，再扫描，最后补存量。
PHASES = ("calibrate", "sweep", "backfill")

#: 关键词/城市选项的分隔符。中文逗号、顿号、分号、换行都算 —— 调用方（尤其 LLM）
#: 不会记得该用哪一个，收下比报错好。
#: ⚠️ **空白不是分隔符**：真实关键词里就有空格（`AI Agent`），按空白切会把它拆成
#: 两个词，于是「AI Agent」永远搜不到东西，而且失败得很安静。
_NAME_SEPARATORS = re.compile(r"[,，;；、\n\r]+")

#: 连续多少条详情失败就停。与 `service_jobs.details()` 同一个阈值：
#: 三条连续失败通常意味着登录态、页面结构或风控状态变了，继续抓只是白烧请求。
FAILURE_LIMIT = 3


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 选项解析


def parse_names(value) -> list[str]:
    """把关键词/城市选项解析成去空、去重的名字列表。

    同时接受数组和分隔字符串：前端表单给的是字符串（"AI Agent,Python"），
    LLM 走 JSON 给的是数组（`["AI Agent", "Python"]`）。两种都收，不让调用方
    去猜格式 —— 猜错的代价是任务创建成功但一个查询都没跑。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = _NAME_SEPARATORS.split(value)
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        raise ValueError("关键词/城市选项只能是数组或逗号分隔的字符串")
    result: list[str] = []
    for item in items:
        name = item.strip()
        if name and name not in result:
            result.append(name)
    return result


def resolve_cities(cfg, requested) -> list[CityConfig]:
    """城市：显式给了就用显式，否则用配置里的。

    平台只认 `boss_code`，所以不在配置里、也不在平台城市表里的名字必须当场报错——
    放过去的结果是 `_params()` 抛「城市 X 缺少 boss_code」，而那时浏览器已经起来了。
    """
    names = parse_names(requested)
    if not names:
        return list(cfg.cities)
    known = {city.name: city for city in cfg.cities}
    result = []
    for name in names:
        city = known.get(name)
        if city is None:
            # CityConfig 会自己从平台城市表补 boss_code（见 models.CityConfig）。
            city = CityConfig(name=name)
            if not city.boss_code:
                raise ValueError(f"城市不在平台城市表里：{name}；请在配置里补 boss_code")
        result.append(city)
    return result


def resolve_keywords(cfg, requested, recency: dict[str, str]) -> list[str]:
    """关键词：显式给了就用显式，否则按「最久没采过」轮转配置里的。

    轮转不是优化而是公平性要求：`max_keywords_per_run` 原先取 `keywords[:3]`，
    于是配置里后面的词从来没被采过（实测连跑四轮插入数 154→217→49→31，
    因为从第二轮起一直在翻同一批结果）。
    """
    names = parse_names(requested)
    if names:
        return names
    return list(rotate_keywords(cfg.keywords, recency, cfg.max_keywords_per_run))


def _int_option(options: dict, name: str, default: int) -> int:
    """取整数选项。`0` 与「没填」必须分开：`detail_limit: 0` 的语义是「不抓正文」，
    不能被 `or` 悄悄变成默认值。"""
    value = options.get(name)
    return default if value is None else int(value)


def plan(ctx, cfg, recency: dict[str, str] | None = None, store=None) -> dict:
    """把任务选项折算成**这一阶段真正会执行什么**。

    单独抽出来的原因有两个：`dry_run` 要靠它给出计划预览（不花一个请求就能
    确认关键词、城市、页数解析对不对），单元测试也靠它验证解析逻辑 —— 不必
    起浏览器、不必 mock 采集器。

    `store` 只在 `backfill` 里用来数「有多少条待补」。
    """
    options = ctx.task["options"]
    phase = str(options.get("phase") or "calibrate").strip().lower()
    if phase not in PHASES:
        raise ValueError("采集会话阶段只能是 calibrate / sweep / backfill")
    limit = int(options.get("limit") or 100)
    if phase == "calibrate":
        # 校准的默认值与 live 脚本一致：翻 5 页、抓 40 条正文。
        pages = _int_option(options, "pages", 5)
        detail_limit = _int_option(options, "detail_limit", 40)
    elif phase == "sweep":
        # 扫描的页数默认跟配置走（`pages_per_query`），不另立一套默认。
        pages = _int_option(options, "pages", cfg.pages_per_query)
        # 每个关键词默认只补 8 条正文：13 个关键词 × 配置里的 30 条，
        # 一天就会把详情配额（封禁红线 150-200 条）烧穿。
        detail_limit = _int_option(options, "detail_limit", 8)
    else:
        pages = 0
        detail_limit = _int_option(options, "detail_limit", 0) or limit
    # 推荐流来自「求职期望」，与搜索参数无关，所以校准不解析城市。
    cities = resolve_cities(cfg, options.get("cities")) if phase == "sweep" else []
    keywords = resolve_keywords(cfg, options.get("keywords"), recency or {}) if phase == "sweep" else []
    output = ""
    if phase == "calibrate":
        # 默认落在项目 output/ 下，文件名带任务 ID：每次校准一份，互不覆盖。
        output = absolute(str(options.get("output") or f"output/calibration-{ctx.ident}.jsonl"),
                          ctx.settings.path.parent)
    result = {"phase": phase, "pages": pages, "detail_limit": detail_limit,
              "keywords": keywords, "cities": [city.name for city in cities],
              "output": output, "limit": limit,
              "expectation_label": str(options.get("expectation_label") or "")}
    if phase == "calibrate":
        # 候选预算是「翻页时先收多少条待抓详情」：留两倍余量，
        # 因为详情会有失败、也会有库里已有正文而跳过的。
        result["candidate_budget"] = min(limit, detail_limit * 2) if detail_limit else limit
    if phase == "sweep":
        result["queries"] = len(cities) * len(keywords)
    if phase == "backfill" and store is not None:
        result["selected_records"] = len(backfill_rows(ctx, store, cfg, detail_limit))
    return result


# ---------------------------------------------------------------- 采集器接线


def fresh(ctx, *, list_source: str | None = None, details: bool | None = None):
    """取一份**当前**配置（热更新从这里进来），再按阶段覆盖两个开关。

    每个查询、每条详情之前都要重新取一次：`collector.config` 是采集器读节奏的
    唯一来源，`configured()` 每次都读最新的 `ctx.config()`。
    """
    cfg = configured(ctx, ctx.config())
    if list_source is not None:
        cfg.list_source = list_source
    if details is not None:
        cfg.collect_details = details
    return cfg


def build_collector(ctx, store, cfg, *, recommendation: bool = False, script: str = ""):
    """建采集器并接上节奏、账本、闸门 —— 三件事缺一不可。

    - `pace`：`boss.py` 里所有节奏点都是 `getattr(self, "pace", None)`，拿不到就
      **什么都不做**（列表和详情都会零间隔连发）。`service_jobs` 是唯一接线的
      调用方，新入口必须自己接。
    - `ledger`：请求账本。没有它就无法回答「今天一共发了多少」。
    - `gate_source`：风控熔断落盘时要写清是谁冻结的，否则人工恢复时不知道
      该找哪个任务。
    - `skip_detail_ids`：启动时快照（库里已有正文的主键），用来把详情配额
      只花在真正缺正文的岗位上。
    """
    from .collectors.boss import BossCollector
    from .collectors.recommendation import RecommendationCollector

    collector = (RecommendationCollector if recommendation else BossCollector)(cfg)
    pacer = RequestPacer(ctx.log)
    # 这个 lambda 是热更新的关键一环：调用时才读 collector.config，
    # 所以任务跑着改 detail_delay_seconds / burst_threshold 会立刻生效。
    collector.pace = lambda kind: pacer.before(kind, collector.config)
    collector.pace_signal = pacer.signal
    collector.online_check = lambda: ctx.settings.read()[0].network_enabled
    collector.ledger = store
    collector.ledger_script = script
    collector.gate_source = f"managed-task:{ctx.ident}"
    collector.skip_detail_ids = store.detailed_ids(cfg.service.detail_after_hours)
    return collector, pacer


def bump(ctx, name: str, amount: int = 1) -> int:
    """给 `ctx.progress` 加一个阶段专属计数。worker 只预置了 processed/total/failed。"""
    ctx.progress[name] = ctx.progress.get(name, 0) + amount
    return ctx.progress[name]


def calibration_record(job, page: int, rank: int) -> dict:
    """一条校准记录：标题 + JD + 招聘信号 + 列表位置。

    字段沿用 `run_session.py::calib_record()`。`list_page` / `list_rank` 是刻意的：
    校准要回答的是「推荐流把什么样的岗位排在第几页第几位」，那需要位置信息，
    不只是岗位内容本身。
    """
    from .analysis.market import hiring_of

    return {
        "job_id": job.source_job_id,
        "title": job.title,
        "company": job.company_name,
        "city": job.city,
        "district": job.district,
        "salary_text": job.salary_text,
        "salary_min_k": job.salary_min_k,
        "salary_max_k": job.salary_max_k,
        "salary_months": job.salary_months,
        "experience": job.experience,
        "education": job.education,
        "company_size": job.company_size,
        "financing_stage": job.financing_stage,
        "industry": job.industry,
        "job_status_desc": job.job_status_desc,
        "recruiter_title": job.recruiter_title,
        "recruiter_active_status": job.recruiter_active_status,
        "recruiter_active_days": job.recruiter_active_days,
        "recruiter_online": job.recruiter_online,
        # hiring_of 是五档在招判定（纯函数）。校准要看的就是「平台推来的岗到底在不在招」，
        # 列表命中不等于在招（死岗位也挂在列表里）。
        "hiring": hiring_of(dict(job.model_dump())),
        "jd_len": len(job.job_description or ""),
        "jd": job.job_description or "",
        "skills": job.skills,
        "welfare": job.welfare,
        "list_page": page,
        "list_rank": rank,
        "retention_status": job.retention_status,
        # 记录时间：多轮校准要能合并，合并时得知道哪条是哪一轮的。
        "calibrated_at": stamp(),
    }


class CalibrationLog:
    """校准记录落 JSONL，写一条 flush 一条。

    为什么每条都 flush：校准是长跑（几十条详情、几十分钟），中途被停止或撞墙
    停机是常态。攒在缓冲区里丢掉的恰恰是最贵的部分 —— 已经花掉请求换来的正文。
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = open(path, "w", encoding="utf-8")
        self.count = 0

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.handle.flush()
        self.count += 1

    def close(self) -> None:
        self.handle.close()


# ---------------------------------------------------------------- 存量选行


def backfill_rows(ctx, store, cfg, limit: int) -> list[dict]:
    """要补正文的存量岗位：active、未失效、有链接、正文为空。

    「缺正文」用 `trim(job_description)=''` 判，与 `storage.detailed_ids()` 同一判据。
    批次 / 指定主键的过滤直接复用 `selection_where()`（kind 传空串 = 不要它自带的
    详情/打标条件），这样 `round_id`、`canonical_ids` 的语义与 `details` 任务一致。
    """
    where, params = selection_where(ctx, store, "", cfg)
    where += (" AND trim(job_description)='' AND retention_status='active'"
              " AND job_invalid=0 AND job_url<>''")
    return [dict(row) for row in store.conn.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY last_seen_at DESC, canonical_id LIMIT ?",
        (*params, int(limit)))]


# ---------------------------------------------------------------- 任务入口


async def run_session(ctx, store, cfg):
    """按 `options["phase"]` 分派。签名与 `service_jobs.recommend()/details()` 一致。"""
    phase = str(ctx.task["options"].get("phase") or "calibrate").strip().lower()
    if phase not in PHASES:
        raise ValueError("采集会话阶段只能是 calibrate / sweep / backfill")
    ctx.report(phase=phase)
    if phase == "calibrate":
        return await calibrate(ctx, store, cfg)
    if phase == "sweep":
        return await sweep(ctx, store, cfg)
    return await backfill(ctx, store, cfg)


async def calibrate(ctx, store, cfg):
    """推荐流采样 + 抓正文，产出校准记录。

    两段式（见模块文档）：先只翻推荐页，翻完再逐条抓详情。第一段期间页面必须
    一直停在推荐流上，否则 `window.scrollTo` 滚在详情页上，等不到推荐响应。
    """
    from .collectors.boss import RiskControlStop
    from .safety import AccessBlocked

    resolved = plan(ctx, cfg, store.keyword_recency(), store)
    pages = resolved["pages"]
    detail_budget = resolved["detail_limit"]
    candidate_budget = resolved["candidate_budget"]
    label = resolved["expectation_label"]
    log = CalibrationLog(Path(resolved["output"]))
    # 推荐流只有原生列表一条路（`recommendation_page` 拦的是页面自己发的响应）。
    collector, pacer = build_collector(ctx, store, fresh(ctx, list_source="native"),
                                       recommendation=True, script="service.session.calibrate")
    collected: list[tuple] = []      # [(job, page, rank)]：待抓正文的候选
    detailed: set[str] = set()       # 本轮已抓过正文的主键，避免同一岗位重复抓
    seen_ids: set[str] = set()       # 已出现过的岗位 ID，用来识别「翻页没翻动」
    consecutive_failures = 0
    ctx.report(total=detail_budget, pages=0, requests=0)
    ctx.log(f"校准开始：推荐流 {pages} 页，最多抓 {detail_budget} 条正文",
            kind="session", plan=resolved)
    try:
        await collector.start()
        # ---- 第一段：只翻页 ----
        for number in range(1, pages + 1):
            collector.config = fresh(ctx, list_source="native")
            ctx.require_access()
            # `ctx.verified` 包住整次翻页：风控在这里暂停任务并等人工验证，
            # 恢复后从同一页继续，不需要我们写重试。
            body = await ctx.verified(
                lambda number=number: collector.recommendation_page(
                    number, expectation_label=label, first=number == 1),
                collector.browser_session, collector.page)
            raw_jobs = (body.get("zpData") or {}).get("jobList") or []
            if not raw_jobs:
                ctx.log(f"推荐第{number}页没有岗位，停止翻页", kind="session")
                break
            page_ids = {raw.get("encryptJobId") for raw in raw_jobs if isinstance(raw, dict)}
            if number > 1 and not page_ids - seen_ids:
                # 翻页没有产生新岗位：继续翻只是重复请求同一批数据。
                ctx.log("推荐下一页没有新岗位，停止翻页以免重复请求", level="warning", kind="session")
                break
            seen_ids.update(page_ids)
            for rank, raw in enumerate(raw_jobs, start=1):
                if not isinstance(raw, dict) or not raw.get("encryptJobId"):
                    continue
                job = collector._parse_api_job(raw, "推荐", rank)
                job.raw["list_source"] = {**(body.get("_list_source") or {}), "page": number}
                job.retention_status = "active" if collector._should_keep(job) else "sample"
                # 列表出现就是「又见到一次」：observed=True 才刷新 seen_count/last_seen_at。
                if store.upsert(job) == "inserted":
                    bump(ctx, "inserted")
                else:
                    bump(ctx, "updated")
                bump(ctx, "list_jobs")
                if job.retention_status == "active":
                    collected.append((job, number, rank))
            ctx.report(pages=number, candidates=len(collected), requests=collector.request_count)
            ctx.log(f"推荐第{number}页：{len(raw_jobs)}条，在招候选 {len(collected)} 条",
                    kind="session")
            if len(collected) >= candidate_budget:
                # 候选够了就不必再翻：多翻一页就是多一个请求。
                break
        # ---- 第二段：逐条抓正文 ----
        ctx.log(f"推荐翻页结束：候选 {len(collected)} 条，开始抓正文", kind="session")
        for offset, (job, page, rank) in enumerate(collected):
            if ctx.progress.get("details", 0) >= detail_budget:
                break
            collector.config = fresh(ctx, list_source="native")
            ctx.require_access()
            key = job.canonical_key()
            if key in detailed:
                # 同轮去重：推荐流会把同一岗位推两次，正文只抓一次。
                ctx.log(f"{key} 本轮已抓过正文，跳过", kind="session")
                continue
            if key in collector.skip_detail_ids:
                # 库里已有正文：直接读出来当校准样本，0 个请求。
                # 这是 `skip_detail_ids` 与「本轮已抓」的区别 —— 前者是历史存量，
                # 后者是本轮刚抓的。
                old = store.conn.execute(
                    "SELECT job_description FROM jobs WHERE canonical_id=?", (key,)).fetchone()
                if old and old["job_description"]:
                    job.job_description = old["job_description"]
                    bump(ctx, "detail_skipped")
            if not job.job_description:
                async def fetch(job=job):
                    # SSR 路径：不依赖 securityId，每岗位 1 个文档请求。
                    # 它内部已经调了 `pace("detail")`，这里不要再加一次。
                    return await collector._collect_ssr_detail(job)
                try:
                    job = await ctx.verified(fetch, collector.browser_session, collector.page)
                    consecutive_failures = 0
                except (RiskControlStop, AccessBlocked):
                    raise
                except (ValueError, RuntimeError) as exc:
                    job.raw["detail_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    ctx.progress["failed"] += 1
                    consecutive_failures += 1
                    ctx.log(f"{key} 详情失败：{exc}", level="warning", kind="session")
                    if consecutive_failures >= FAILURE_LIMIT:
                        raise RuntimeError(
                            f"连续 {FAILURE_LIMIT} 条详情失败，任务停止；请检查登录态与页面结构") from exc
            # observed=False：补正文不是「又出现在列表里」，不能再刷一次
            # seen_count / last_seen_at（那会把「最近一次见到」这个语义弄失真）。
            store.upsert(job, observed=False)
            if job.job_description:
                detailed.add(key)
                log.write(calibration_record(job, page, rank))
                bump(ctx, "details")
            ctx.report(processed=offset + 1, details=ctx.progress.get("details", 0),
                       detail_skipped=ctx.progress.get("detail_skipped", 0),
                       requests=collector.request_count)
        ctx.report(output=resolved["output"], calibration_records=log.count,
                   requests=collector.request_count, pacer_requests=pacer.requests,
                   pacer_details=pacer.details)
        ctx.log(f"校准完成：{log.count} 条记录 -> {resolved['output']}", kind="session")
        return dict(ctx.progress)
    finally:
        log.close()
        await collector.close()


async def sweep(ctx, store, cfg):
    """关键词轮转扫描 + 就地补详情。

    直接复用 `collector.collect(city, keyword, filters, pages, detail_limit)`：
    翻页、去重、详情触发、风控熔断都已在采集器里实现，这里只负责编排查询序列
    与计数。**不建采集批次**：批次的指纹是整份 `query_plan(cfg)`，而这里的
    城市/关键词是调用方给的子集，硬建批次会让 `job-radar rounds` 的语义失真。
    """
    from .collectors.boss import RiskControlStop
    from .safety import AccessBlocked

    resolved = plan(ctx, cfg, store.keyword_recency(), store)
    pages = resolved["pages"]
    detail_limit = resolved["detail_limit"]
    # 直接用 plan() 解析好的关键词：计划预览与实际执行必须是同一份，
    # 否则 dry_run 报的查询数和真跑的会对不上。
    cities = resolve_cities(cfg, ctx.task["options"].get("cities"))
    keywords = resolved["keywords"]
    # `collect_details` 由 detail_limit 决定：写 0 就是不补正文，不用再去改配置。
    # 配置里 `collect_details: false` 时也必须打开，否则「就地补详情」永远不发生。
    collector, pacer = build_collector(ctx, store, fresh(ctx, details=detail_limit > 0),
                                       script="service.session.sweep")
    filters = (cfg.filters or [SearchFilters()])[0]
    tasks = [(city, word) for city in cities for word in keywords]
    # 打散顺序：同一城市、同一关键词连着打是最容易被识别的节奏。
    random.shuffle(tasks)
    detailed: set[str] = set()       # 本轮抓过正文的主键（跨关键词共享，去重用）
    query_failures = 0               # 连续失败的查询数
    ctx.report(total=len(tasks), queries_total=len(tasks), queries_done=0,
               keywords=keywords, cities=[city.name for city in cities])
    ctx.log(f"扫描开始：{len(cities)} 城市 × {len(keywords)} 关键词 = {len(tasks)} 个查询，"
            f"每词 {pages} 页、最多补 {detail_limit} 条正文",
            kind="session", plan=resolved)

    def save_list(job):
        """列表数据先落库：详情触发验证或停机时，这一页已经搜到的岗位仍可恢复。"""
        if store.upsert(job) == "inserted":
            bump(ctx, "inserted")
        else:
            bump(ctx, "updated")
        bump(ctx, "list_jobs")

    try:
        await collector.start()
        for index, (city, word) in enumerate(tasks, start=1):
            collector.config = fresh(ctx, details=detail_limit > 0)
            ctx.require_access()
            ctx.log(f"扫描 {city.name} / {word}", kind="session")
            collector.on_list = save_list

            async def query(city=city, word=word):
                async for job in collector.collect(city, word, filters, pages, detail_limit):
                    key = job.canonical_key()
                    if job.job_description.strip():
                        # 详情正文落库；observed=False 不重复累加 seen_count。
                        store.upsert(job, observed=False)
                        if key not in detailed:
                            detailed.add(key)
                            bump(ctx, "details")
                        # 采集器抓成功后已把它加进 skip_detail_ids，
                        # 所以下一个关键词再搜到同一岗位不会重复抓详情。
                        collector.skip_detail_ids.add(key)
                    if job.raw.get("detail_error"):
                        ctx.progress["failed"] += 1
                        ctx.log(f"{key} 详情失败：{job.raw['detail_error']}",
                                level="warning", kind="session")
                    ctx.report(requests=collector.request_count)
                return True

            try:
                await ctx.verified(query, collector.browser_session, collector.page)
            except (RiskControlStop, AccessBlocked):
                raise
            except (ValueError, RuntimeError) as exc:
                # 一条查询失败不该拖垮整轮：39 个查询里一次网络抖动就全停，代价太大
                # （2026-10-19 实测：`TypeError: Failed to fetch` 让整轮扫描在 4/39 处
                # 直接失败）。但**连续**失败仍然停 —— 那说明登录态、页面结构或风控
                # 状态变了，继续只是白烧请求。阈值与详情失败共用 `FAILURE_LIMIT`。
                query_failures += 1
                ctx.progress["failed"] += 1
                ctx.log(f"查询失败 {city.name}/{word}：{exc}", level="warning", kind="session")
                if query_failures >= FAILURE_LIMIT:
                    raise RuntimeError(
                        f"连续 {FAILURE_LIMIT} 个查询失败，任务停止；请检查登录态与页面结构"
                    ) from exc
            else:
                query_failures = 0
            # 轮转只关心「这个词多久没碰过」，不动翻页进度。
            store.touch_keyword_progress(city.name, word)
            ctx.report(queries_done=index, processed=index,
                       requests=collector.request_count, details=ctx.progress.get("details", 0))
            ctx.log(f"查询完成 {city.name}/{word}（{index}/{len(tasks)}）", kind="session")
        ctx.report(requests=collector.request_count, pacer_requests=pacer.requests,
                   pacer_details=pacer.details)
        ctx.log(f"扫描完成：{len(tasks)} 个查询，{ctx.progress.get('list_jobs', 0)} 条列表命中，"
                f"{ctx.progress.get('details', 0)} 条正文", kind="session")
        return dict(ctx.progress)
    finally:
        await collector.close()


async def backfill(ctx, store, cfg):
    """给 active 且缺正文的存量岗位补详情（走 SSR 路径，不依赖 securityId）。

    为什么必须有这条路：详情接口要列表响应里的 `securityId`，而它会被轮换、
    也会被历史剪枝删掉。存量岗位既不在搜索结果里、也没有凭据，只能打开
    `/job_detail/{id}.html` 从 SSR HTML 里取正文。
    """
    from .collectors.boss import RiskControlStop
    from .safety import AccessBlocked

    resolved = plan(ctx, cfg, store.keyword_recency(), store)
    budget = resolved["detail_limit"]
    rows = backfill_rows(ctx, store, cfg, budget)
    collector, pacer = build_collector(ctx, store, fresh(ctx),
                                       script="service.session.backfill")
    consecutive_failures = 0
    ctx.report(total=len(rows), requests=0, details=0)
    ctx.log(f"补正文开始：待补 {len(rows)} 条", kind="session", plan=resolved)
    try:
        if not rows:
            ctx.log("当前没有缺正文的在招岗位", kind="session")
            return dict(ctx.progress)
        await collector.start()
        for offset, row in enumerate(rows):
            collector.config = fresh(ctx)
            ctx.require_access()
            job = row_to_job(row)
            key = job.canonical_key()
            if not job.job_url:
                ctx.progress["failed"] += 1
                ctx.report(processed=offset + 1)
                continue

            async def fetch(job=job):
                # SSR 路径内部已调 `pace("detail")`，不要重复加。
                return await collector._collect_ssr_detail(job)

            try:
                job = await ctx.verified(fetch, collector.browser_session, collector.page)
            except (RiskControlStop, AccessBlocked):
                raise
            except (ValueError, RuntimeError) as exc:
                ctx.progress["failed"] += 1
                consecutive_failures += 1
                ctx.log(f"{key} 补正文失败：{exc}", level="warning", kind="session")
                if consecutive_failures >= FAILURE_LIMIT:
                    raise RuntimeError(
                        f"连续 {FAILURE_LIMIT} 条详情失败，任务停止；请检查登录态与页面结构") from exc
            else:
                consecutive_failures = 0
                # observed=False：补正文不是「又出现在列表里」。
                store.upsert(job, observed=False)
                bump(ctx, "details")
                ctx.log(f"已补 {key} 的正文（{len(job.job_description)} 字）", kind="session")
            ctx.report(processed=offset + 1, details=ctx.progress.get("details", 0),
                       requests=collector.request_count)
        ctx.report(requests=collector.request_count, pacer_requests=pacer.requests,
                   pacer_details=pacer.details)
        ctx.log(f"补正文完成：{ctx.progress.get('details', 0)} 条", kind="session")
        return dict(ctx.progress)
    finally:
        await collector.close()
