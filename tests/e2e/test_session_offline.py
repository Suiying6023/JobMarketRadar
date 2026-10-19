"""采集会话（`kind="session"`）的离线测试。

**不联网、不起浏览器**：`session.build_collector()` 被换成替身，所以这里测的是
`session.py` 自己的编排逻辑 ——

- 阶段分派与选项解析（关键词 / 城市 / 页数 / 详情条数）
- 校准的两段式顺序（先翻完推荐页，再抓正文）与同轮详情去重
- `observed=False` 语义（补正文不刷 `seen_count` / `last_seen_at`）
- 补正文只选 active 且缺正文的行

真实采集链路（真的发 HTTP、真的解码、真的记账）由 `test_pipeline_e2e.py` 覆盖；
这里刻意把网络整层替掉，因为它要守的是**编排**，不是请求本身。
"""
from __future__ import annotations

import asyncio
import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_radar import session
from job_radar.collectors.boss_payload import parse_list_job
from job_radar.models import CityConfig, CollectionConfig, SearchFilters
from job_radar.storage import JobStore


HANGZHOU = {"name": "杭州", "boss_code": "101210100"}
SHANGHAI = {"name": "上海", "boss_code": "101020100"}


def make_config(**overrides) -> CollectionConfig:
    payload = {
        "cities": [HANGZHOU, SHANGHAI],
        "keywords": ["AI Agent", "智能体开发", "大模型应用开发", "Python"],
        "network_enabled": True,
        # 节奏在离线测试里没有意义：这些值只影响挂钟时间，不影响编排。
        "request_delay_seconds": [0.0, 0.0],
        "detail_delay_seconds": [0.0, 0.0],
        "detail_batch": 50,
        "detail_cooldown_seconds": [0.0, 0.0],
        "burst_threshold": 100,
        "burst_penalty_seconds": [0.0, 0.0],
        "pages_per_query": 2,
        "max_keywords_per_run": 2,
        **overrides,
    }
    return CollectionConfig.model_validate(payload)


def raw_job(job_id: str, *, title: str = "AI Agent 开发工程师") -> dict:
    """列表响应里的最小岗位记录。字段名必须是平台原始形状（走真解析器）。"""
    return {
        "encryptJobId": job_id,
        "jobName": title,
        "brandName": "示例科技",
        "brandIndustry": "人工智能",
        "brandScaleName": "100-499人",
        "brandStageName": "B轮",
        "encryptBrandId": "brand" + job_id,
        "cityName": "杭州",
        "areaDistrict": "滨江区",
        "businessDistrict": "江南大道",
        "salaryDesc": "15-25K",
        "jobExperience": "经验不限",
        "jobDegree": "本科",
        "skills": ["Python", "Agent"],
        "welfareList": ["五险一金"],
        "bossName": "王女士",
        "bossTitle": "HR",
        "bossOnline": True,
    }


class FakeSettings:
    """`ctx.settings` 的最小替身：只提供配置路径和当前配置。"""

    def __init__(self, path: Path, config: CollectionConfig):
        self.path = path
        self._config = config

    def read(self):
        return self._config, "offline-revision"


class FakeContext:
    """`worker.TaskContext` 的最小替身。

    `verified()` 直接执行传入的操作：风控暂停/等待人工验证属于 `worker.py` 的行为，
    这里替掉它是为了让测试只关注 session 的编排。
    """

    def __init__(self, tmp_path: Path, options: dict, *, config: CollectionConfig | None = None):
        self.task = {"kind": "session",
                     "options": {"limit": 100, "dry_run": False, "round_id": "", **options}}
        self.ident = "sessionoffline01"
        self.progress = {"processed": 0, "total": 0, "failed": 0}
        self.database = tmp_path / "jobs.sqlite3"
        self._config = config or make_config()
        self.settings = FakeSettings(tmp_path / "config.local.yaml", self._config)
        self.events: list[tuple] = []

    def config(self):
        return self._config

    def log(self, message, *, level="info", kind="log", **data):
        self.events.append((kind, level, str(message), data))

    def report(self, **values):
        self.progress.update(values)

    def require_access(self):
        return None

    async def verified(self, operation, browser, page):
        return await operation()


class FakeCollector:
    """替身采集器：只实现 `session.py` 真正调用的那几个入口。

    `trace` 记录**调用顺序**，用来钉住「先翻完推荐页，再抓正文」这条硬要求
    （见模块文档里那个 30 秒超时的 bug）。
    """

    def __init__(self, *, pages=None, list_jobs=None):
        self.pages = pages or {}            # {页号: [raw, ...]}，推荐流
        self.list_jobs = list_jobs or {}    # {关键词: [raw, ...]}，搜索结果
        self.config = None
        self.request_count = 0
        self.skip_detail_ids: set[str] = set()
        self.detail_calls: list[str] = []
        self.queries: list[tuple] = []
        self.trace: list[tuple] = []
        self.browser_session = None
        self.page = None
        self.on_list = None

    async def start(self):
        self.trace.append(("start", ""))

    async def close(self):
        self.trace.append(("close", ""))

    # ---- 真解析器：不做替身，否则测不到真实字段映射 ----

    def _parse_api_job(self, raw, keyword, rank):
        return parse_list_job(raw, keyword, rank)

    def _should_keep(self, job):
        return True

    # ---- 网络入口：全部替身 ----

    async def recommendation_page(self, number, *, expectation_label="", first=False):
        self.request_count += 1
        self.trace.append(("page", number))
        assert first == (number == 1), "只有第 1 页能带 first=True（它负责导航/切期望）"
        return {"code": 0,
                "zpData": {"jobList": list(self.pages.get(number, []))},
                "_list_source": {"source_kind": "recommendation", "page": number}}

    async def _collect_ssr_detail(self, job):
        self.request_count += 1
        self.detail_calls.append(job.canonical_key())
        self.trace.append(("detail", job.canonical_key()))
        job.job_description = f"{job.source_job_id} 的正文"
        job.recruiter_active_status = "刚刚活跃"
        job.detail_fetched_at = "2026-10-19T12:00:00+00:00"
        job.raw["detail"] = {"jobInfo": {"encryptId": job.source_job_id}}
        return job

    async def collect(self, city, keyword, filters, pages, detail_limit):
        """复刻真实采集器的编排：先 `on_list` 落列表，再按预算/去重抓详情。"""
        self.trace.append(("query", keyword))
        self.queries.append((city.name, keyword, pages, detail_limit))
        detailed = 0
        for raw in self.list_jobs.get(keyword, []):
            job = self._parse_api_job(raw, keyword, 1)
            job.retention_status = "active"
            if self.on_list is not None:
                self.on_list(job)
            already = job.canonical_key() in self.skip_detail_ids
            if detail_limit and self.config.collect_details and detailed < detail_limit and not already:
                job = await self._collect_ssr_detail(job)
                detailed += 1
                self.skip_detail_ids.add(job.canonical_key())
            yield job


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite3") as handle:
        yield handle


@pytest.fixture
def install_fake(monkeypatch):
    """把 `session.build_collector()` 换成返回指定替身的版本。

    替身也要拿 `skip_detail_ids`（库里已有正文的主键）：那是「已有正文就不发详情
    请求」的依据，不灌进去，测出来的去重就是假的。
    """

    def install(collector: FakeCollector):
        pacer = types.SimpleNamespace(requests=0, details=0)

        def build(ctx, store, cfg, *, recommendation=False, script=""):
            collector.config = cfg
            collector.skip_detail_ids = store.detailed_ids(cfg.service.detail_after_hours)
            collector.ledger_script = script
            collector.recommendation = recommendation
            return collector, pacer

        monkeypatch.setattr(session, "build_collector", build)
        # 扫描会把「城市×关键词」打散顺序（真实运行时是刻意的节奏抖动）。
        # 测试里关掉它：断言要能复现，抖动不该决定用例跑哪一条分支。
        monkeypatch.setattr(session.random, "shuffle", lambda items: None)
        return collector

    return install


# ---------------------------------------------------------------- 选项解析


def test_plan_resolves_keywords_cities_and_limits(tmp_path):
    """阶段选项解析：数组/分隔字符串都收，0 与「没填」必须分开。"""
    ctx = FakeContext(tmp_path, {"phase": "sweep", "pages": 3, "detail_limit": 0,
                                 "keywords": "AI Agent，Python、RAG", "cities": ["杭州", "上海"]})
    resolved = session.plan(ctx, ctx.config())

    assert resolved["phase"] == "sweep"
    assert resolved["keywords"] == ["AI Agent", "Python", "RAG"]
    assert resolved["cities"] == ["杭州", "上海"]
    assert resolved["pages"] == 3
    # `detail_limit=0` 的语义是「只扫不抓正文」，不能被默认值 8 顶掉。
    assert resolved["detail_limit"] == 0
    assert resolved["queries"] == 6

    # 不填关键词：按「最久没采过」轮转配置里的（max_keywords_per_run=2）。
    default = FakeContext(tmp_path, {"phase": "sweep"})
    resolved = session.plan(default, default.config(), recency={"AI Agent": "2026-10-19 10:00:00"})
    assert resolved["keywords"] == ["智能体开发", "大模型应用开发"], "采过的词要排到后面"
    assert resolved["pages"] == 2, "页数默认取配置的 pages_per_query"


def test_plan_defaults_per_phase_and_rejects_unknown_values(tmp_path):
    """每个阶段的默认值各自成立；非法阶段/城市要当场报错，而不是起完浏览器再炸。"""
    calibrate = FakeContext(tmp_path, {"phase": "calibrate"})
    resolved = session.plan(calibrate, calibrate.config())
    assert resolved["pages"] == 5 and resolved["detail_limit"] == 40
    assert resolved["output"].endswith("calibration-sessionoffline01.jsonl")
    assert resolved["cities"] == [], "推荐流来自求职期望，与城市无关"

    backfill = FakeContext(tmp_path, {"phase": "backfill", "limit": 60})
    resolved = session.plan(backfill, backfill.config())
    assert resolved["detail_limit"] == 60, "补正文不填条数时取数量上限"

    with pytest.raises(ValueError):
        session.plan(FakeContext(tmp_path, {"phase": "unknown"}), make_config())
    with pytest.raises(ValueError):
        session.plan(FakeContext(tmp_path, {"phase": "sweep", "cities": ["火星"]}), make_config())


def test_parse_names_accepts_both_shapes():
    assert session.parse_names(None) == []
    assert session.parse_names("") == []
    assert session.parse_names(["AI Agent", " Python ", ""]) == ["AI Agent", "Python"]
    assert session.parse_names("AI Agent,Python；RAG、Coze\nJava") == ["AI Agent", "Python", "RAG", "Coze", "Java"]
    # 去重保序：同一关键词写两遍不该变成两个查询。
    assert session.parse_names("Python,python,Python") == ["Python", "python"]
    with pytest.raises(ValueError):
        session.parse_names(42)


# ---------------------------------------------------------------- 校准


def test_calibrate_pages_first_then_fetches_details(tmp_path, store, install_fake):
    """两段式 + 同轮详情去重。

    这条守的是真实踩过的 bug：边翻页边抓详情时，`_collect_ssr_detail()` 的
    `page.goto` 会把页面带离推荐流，下一页的 `window.scrollTo` 就等不到推荐响应
    （实测 `recommend_error page=2 TimeoutError: Timeout 30000ms exceeded`）。
    所以 trace 必须是「所有 page 都在所有 detail 之前」。

    第 2 页故意重复第 1 页的 A 岗位：正文只该抓一次，校准记录也只该有一条。
    """
    collector = install_fake(FakeCollector(pages={
        1: [raw_job("jobA"), raw_job("jobB")],
        2: [raw_job("jobA"), raw_job("jobC")],
    }))
    ctx = FakeContext(tmp_path, {"phase": "calibrate", "pages": 2, "detail_limit": 3,
                                 "output": str(tmp_path / "calibration.jsonl")})

    result = asyncio.run(session.run_session(ctx, store, make_config()))

    stages = [kind for kind, _ in collector.trace]
    assert stages[:2] == ["start", "page"], stages
    assert stages[-1] == "close"
    last_page = max(index for index, kind in enumerate(stages) if kind == "page")
    first_detail = min(index for index, kind in enumerate(stages) if kind == "detail")
    assert last_page < first_detail, f"翻页必须全部先做完：{collector.trace}"

    # 同轮去重：A 出现在两页里，正文只抓一次。
    assert collector.detail_calls == ["boss:jobA", "boss:jobB", "boss:jobC"]
    assert result["details"] == 3

    records = [json.loads(line) for line in
               (tmp_path / "calibration.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["job_id"] for record in records] == ["jobA", "jobB", "jobC"]
    assert [record["list_page"] for record in records] == [1, 1, 2]
    assert [record["list_rank"] for record in records] == [1, 2, 2]
    assert all(record["jd"].endswith("的正文") for record in records)
    assert all(record["hiring"]["level"] for record in records)
    assert all(record["calibrated_at"] for record in records)

    # 入库：3 条都有正文。A 的 seen_count 是 2（两页各出现一次），
    # 而**抓正文那次 upsert 不再加 1**（observed=False）。
    rows = {row[0]: row for row in store.conn.execute(
        "SELECT canonical_id, job_description, seen_count FROM jobs")}
    assert set(rows) == {"boss:jobA", "boss:jobB", "boss:jobC"}
    assert all(row[1] for row in rows.values())
    assert rows["boss:jobA"][2] == 2


def test_calibrate_reuses_stored_body_without_a_request(tmp_path, store, install_fake):
    """库里已有正文的岗位直接读出来当样本，不花请求（`skip_detail_ids` 的用途）。"""
    collector = install_fake(FakeCollector(pages={1: [raw_job("jobA")]}))
    stored = parse_list_job(raw_job("jobA"), "旧关键词", 1)
    stored.job_description = "库里早就存好的正文"
    # 正文时间必须落在 `service.detail_after_hours`（默认 168 小时）之内，
    # 否则 detailed_ids() 不会把它算成「已有正文」。
    stored.detail_fetched_at = datetime.now(timezone.utc).isoformat()
    store.upsert(stored)

    ctx = FakeContext(tmp_path, {"phase": "calibrate", "pages": 1, "detail_limit": 5,
                                 "output": str(tmp_path / "calibration.jsonl")})
    result = asyncio.run(session.run_session(ctx, store, make_config()))

    assert collector.detail_calls == [], "有正文就不该再发详情请求"
    assert result["detail_skipped"] == 1
    record = json.loads((tmp_path / "calibration.jsonl").read_text(encoding="utf-8").strip())
    assert record["jd"] == "库里早就存好的正文"


# ---------------------------------------------------------------- 扫描


def test_sweep_runs_each_city_keyword_and_dedupes_details(tmp_path, store, install_fake):
    """扫描：城市×关键词逐个跑；同一岗位被两个关键词搜到，正文只抓一次。"""
    collector = install_fake(FakeCollector(list_jobs={
        "AI Agent": [raw_job("jobA")],
        "Python": [raw_job("jobA"), raw_job("jobB")],
    }))
    ctx = FakeContext(tmp_path, {"phase": "sweep", "pages": 2, "detail_limit": 1,
                                 "keywords": ["AI Agent", "Python"], "cities": ["杭州"]})

    result = asyncio.run(session.run_session(ctx, store, make_config()))

    # 每个查询都用同一套页数/详情预算；城市被解析成 CityConfig（带 boss_code）。
    assert sorted(collector.queries) == [("杭州", "AI Agent", 2, 1), ("杭州", "Python", 2, 1)]
    assert result["queries_done"] == 2 and result["queries_total"] == 2
    # jobA 在第一个关键词里已经抓过正文，第二个关键词不该再抓。
    assert collector.detail_calls == ["boss:jobA", "boss:jobB"]
    assert result["details"] == 2

    rows = {row[0]: row for row in store.conn.execute(
        "SELECT canonical_id, job_description, seen_count FROM jobs")}
    assert all(row[1] for row in rows.values())
    # 列表观察次数：A 两个关键词各出现一次 → 2；B 一次 → 1。
    assert rows["boss:jobA"][2] == 2
    assert rows["boss:jobB"][2] == 1
    # 轮转进度被推进（下次轮到别的关键词）。
    assert {row[0] for row in store.conn.execute("SELECT keyword FROM keyword_progress")} == \
        {"AI Agent", "Python"}


def test_sweep_with_zero_detail_limit_only_collects_lists(tmp_path, store, install_fake):
    """`detail_limit=0` 是有效取值：只扫列表，一条详情请求都不发。"""
    collector = install_fake(FakeCollector(list_jobs={"AI Agent": [raw_job("jobA")]}))
    ctx = FakeContext(tmp_path, {"phase": "sweep", "pages": 1, "detail_limit": 0,
                                 "keywords": ["AI Agent"], "cities": ["杭州"]})

    asyncio.run(session.run_session(ctx, store, make_config(collect_details=False)))

    assert collector.detail_calls == []
    assert collector.queries == [("杭州", "AI Agent", 1, 0)]
    row = store.conn.execute(
        "SELECT job_description, seen_count FROM jobs WHERE canonical_id='boss:jobA'").fetchone()
    assert row["job_description"] == "" and row["seen_count"] == 1


# ---------------------------------------------------------------- 补正文


def test_backfill_selects_active_rows_without_body_only(tmp_path, store, install_fake):
    """补正文只碰「active + 未失效 + 缺正文」的行，且不刷 `seen_count` / `last_seen_at`。"""
    def seed(job_id, **fields):
        job = parse_list_job(raw_job(job_id), "AI Agent", 1)
        for name, value in fields.items():
            setattr(job, name, value)
        store.upsert(job)
        return job.canonical_key()

    missing = seed("jobMissing")                       # 该补
    with_body = seed("jobWithBody", job_description="已有正文")
    sample = seed("jobSample", retention_status="sample")
    closed = seed("jobClosed", job_invalid=True)
    before = {row["canonical_id"]: (row["seen_count"], row["last_seen_at"])
              for row in store.conn.execute("SELECT canonical_id, seen_count, last_seen_at FROM jobs")}

    collector = install_fake(FakeCollector())
    ctx = FakeContext(tmp_path, {"phase": "backfill", "limit": 10})
    result = asyncio.run(session.run_session(ctx, store, make_config()))

    assert collector.detail_calls == [missing], "只补 active 且缺正文的那一条"
    assert result["total"] == 1 and result["details"] == 1

    after = {row["canonical_id"]: (row["seen_count"], row["last_seen_at"])
             for row in store.conn.execute("SELECT canonical_id, seen_count, last_seen_at FROM jobs")}
    assert after[missing] == before[missing], "补正文不是「又出现在列表里」"
    assert store.conn.execute(
        "SELECT job_description FROM jobs WHERE canonical_id=?", (missing,)).fetchone()[0]
    for untouched in (with_body, sample, closed):
        assert store.conn.execute(
            "SELECT job_description FROM jobs WHERE canonical_id=?", (untouched,)).fetchone()[0] in ("", "已有正文")
