"""请求账本与熔断的回归测试。

这两个机制是账号安全的最后防线，且都是"平时看不出坏了"的类型——
账本记漏只会让日线偏松，熔断失效只在真封禁时才显形。必须有测试钉住。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig
from job_radar.storage import JobStore


@pytest.fixture(autouse=True)
def risk_dump_dir(tmp_path, monkeypatch):
    """同 test_risk_control：别把假 dump 写进真实取证目录。"""
    monkeypatch.setattr(boss_module, "RISK_DUMP_DIR", tmp_path / "risk_dumps")


class FakePage:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self.body = body if body is not None else {"code": 0, "zpData": {"jobList": []}}
        self.url = ORIGIN
        self.requested_urls: list[str] = []

    async def evaluate(self, _script: str, arg=None) -> dict:
        if arg is not None:
            self.requested_urls.append(arg["url"] if isinstance(arg, dict) else arg)
        return {"status": self.status, "text": json.dumps(self.body, ensure_ascii=False)}

    async def wait_for_timeout(self, _ms: int) -> None:
        return None


def make_collector(page: FakePage, store: JobStore | None = None, **overrides):
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        **overrides,
    }
    collector = BossCollector(CollectionConfig.model_validate(payload))
    collector.page = page  # type: ignore[assignment]
    if store is not None:
        collector.ledger = store
        collector.ledger_script = "test"
    return collector


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "t.sqlite3") as s:
        yield s


def test_ledger_records_each_request(store):
    page = FakePage()
    collector = make_collector(page, store)
    for _ in range(3):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    counts = store.request_counts()
    assert counts["list"] == 3
    assert counts["total"] == 3


def test_ledger_classifies_detail_vs_list(store):
    """详情和列表的风险差异极大（风控几乎全在详情），账上必须分得开。"""
    page = FakePage(body={"code": 0, "zpData": {"jobInfo": {}}})
    collector = make_collector(page, store)
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json?securityId=x"))
    counts = store.request_counts()
    assert counts["list"] == 1
    assert counts["detail"] == 1


def test_daily_budget_is_cross_process(store):
    """日线是跨进程的：账本里已有的量要算进来，不能只看本进程计数。

    ⚠️ 2026-08-31 起日线是**降速线**而非停线，所以这里断言的是"另一个脚本的量
    也能把本进程推入降速档"，而不是"第一次请求就抛异常"。
    跨进程这一点没变，只是越线后的动作从"停"改成了"慢"。
    """
    for _ in range(5):
        store.log_request("detail", script="other_script")
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=5)
    # 本进程一次请求都还没发，但账本里已有 5 次 → 已越线，应降速。
    assert collector.request_count == 0
    assert collector.over_quota_factor() > 1.0

    # 越线不阻断请求：请求照发，只是节奏变慢。
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1


def test_per_run_cap_still_hard_stops(store):
    """`max_requests_per_run` 仍是硬停线，别跟着日线一起放开。

    两者性质不同：日线是我们自设的风控预算（可以降速越过），
    单轮上限只是"别让一个进程无限跑"（进程越久浏览器状态越可能漂），
    调用方重起一段就能继续。合并这两个概念会让"重起一段"失去意义。
    """
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=5000)
    collector.config.max_requests_per_run = 1
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "单次运行请求上限" in str(err.value)


def test_daily_budget_allows_under_cap(store):
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=10)
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1


def test_ledger_absent_does_not_break_collection():
    """没注入账本时行为不变——账本不能成为采集的硬依赖。"""
    page = FakePage()
    collector = make_collector(page)  # 不传 store
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1
    assert collector.kind_counts == {"list": 1}


def test_ledger_write_failure_does_not_break_collection(store):
    """账本写失败也不能打断采集：请求已经发出去了，记不上账是观测问题。"""
    page = FakePage()
    collector = make_collector(page, store)

    class BrokenLedger:
        def log_request(self, *a, **k):
            raise RuntimeError("disk full")

        def request_counts(self, *a, **k):
            return {"total": 0}

    collector.ledger = BrokenLedger()  # type: ignore[assignment]
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1


def test_risk_outcome_backfilled_onto_last_row(store):
    """风控结论只能事后回填：记账在请求发出前，那时还不知道响应是什么。

    注意 message 故意不含 RISK_TEXTS 关键字——含关键字会先命中上面那条
    message 检查（记为 throttled），走不到 RISK_CODES 分支。两条分支的
    先后顺序是对的（文案检查是节流的唯一防线），这里要测的是后者。
    """
    page = FakePage(body={"code": 32, "message": "已暂时被禁止使用"})
    collector = make_collector(page, store)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    row = store.conn.execute(
        "SELECT kind, outcome, code FROM request_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "detail"
    assert row["outcome"] == "risk"
    assert row["code"] == 32


def test_request_counts_scoped_to_day(store):
    """跨天不能混算——否则昨天的量会吃掉今天的额度。"""
    store.log_request("detail", script="t")
    store.conn.execute("UPDATE request_log SET day = '2000-01-01'")
    store.conn.commit()
    assert store.request_counts().get("total", 0) == 0
    assert store.request_counts("2000-01-01")["detail"] == 1


def test_total_excludes_refresh_chain(store):
    """`total` 只算采集链路，不含状态刷新链路的 detail_page。

    两条链路是不同登录态（带 cookie vs 免登录）+ 不同出口（家宽 vs 波尔机房 IP），
    风控按「IP + 账号信任度」判，各自独立记账。合并计数时刷 100 条状态
    （1400 次 detail_page）就能把采集侧日线 1000 顶满，而那些请求从未经过
    采集账号或家宽 IP。
    """
    store.log_request("list", script="expand_list")
    store.log_request("detail", script="backfill_details")
    store.log_request("security_check", script="backfill_details")
    for _ in range(14):
        store.log_request("detail_page", script="refresh_status_loop")

    counts = store.request_counts()
    assert counts["total"] == 3, "detail_page 不该计入采集链路日线"
    assert counts["detail_page"] == 14, "刷新链路的量仍要可见，只是不参与 total"


def test_daily_budget_ignores_refresh_chain(store):
    """采集侧的日线**降速**不该被刷新链路的量触发。

    ⚠️ 2026-08-31 语义变更：日线从硬停线改成降速线（用户要求「达到配额但还没
    风控就降低速度运行」）。理由是 900 这个数不是平台给的，而是我们按
    「未触发封禁的那轮」反推的自设预算；真正的风控信号（code=36/32、验证页）
    仍然立刻停。所以这里断言的是 `over_quota_factor`，不再是抛异常。
    """
    page = FakePage(body={"code": 0, "zpData": {}})
    collector = make_collector(page)
    collector.ledger = store
    collector.config.max_requests_per_day = 5

    for _ in range(50):
        store.log_request("detail_page", script="refresh_status_loop")

    # 50 次刷新请求远超日线 5，但采集链路今天还没发过任何请求 → 不该降速。
    assert collector.over_quota_factor() == 1.0
    collector._check_daily_budget()   # 不抛

    for _ in range(5):
        store.log_request("detail", script="backfill_details")
    assert collector.over_quota_factor() > 1.0, "越线后必须降速"
    collector._check_daily_budget()   # 仍然不抛——降速不是停机


def test_over_quota_slows_down_but_never_stops(store):
    """越线只降速、永不抛异常；倍数随超出比例阶梯上升。

    钉死"永不抛"这一点：把降速写成异常会让越线表现得跟风控一样，
    而两者要求的人工动作完全不同（一个不需要人，一个必须立刻处理）。
    """
    page = FakePage(body={"code": 0, "zpData": {}})
    collector = make_collector(page)
    collector.ledger = store
    collector.config.max_requests_per_day = 100

    seen = []
    for target, expect in ((100, 2.0), (130, 3.0), (160, 4.0), (220, 6.0)):
        while store.request_counts().get("total", 0) < target:
            store.log_request("detail", script="t")
        f = collector.over_quota_factor()
        seen.append(f)
        assert f == expect, f"用量 {target} 时应降速 {expect}x，实际 {f}x"
        collector._check_daily_budget()   # 任何档位都不抛
    assert seen == sorted(seen), "倍数必须单调不减"


def test_collect_reraises_risk_control_stop():
    """collect() 里的 `except Exception` 曾吞掉 RiskControlStop（它继承 RuntimeError），
    真封禁时循环还在往下抓，持续撞墙加重惩罚。必须原样上抛。"""
    page = FakePage(body={
        "code": 0,
        "zpData": {"jobList": [{
            "encryptJobId": "j1", "jobName": "AI Agent 工程师",
            "brandName": "示例科技", "cityName": "杭州",
            "salaryDesc": "15-25K", "jobExperience": "经验不限",
            "jobDegree": "本科", "securityId": "s1",
        }]},
    })
    collector = make_collector(page, collect_details=True)

    async def boom(job):
        raise RiskControlStop("命中平台风控 code=32")

    collector._collect_detail = boom  # type: ignore[assignment]

    async def drain():
        from job_radar.models import CityConfig, SearchFilters
        async for _ in collector.collect(
            CityConfig(name="杭州", boss_code="101210100"),
            "AI Agent", SearchFilters(), pages=1, detail_limit=10,
        ):
            pass

    async def run():
        collector._goto_search = lambda *a, **k: asyncio.sleep(0)  # type: ignore[assignment]
        await drain()

    with pytest.raises(RiskControlStop):
        asyncio.run(run())


def test_collect_circuit_breaker_on_code37():
    """collect 路径的 code=37 熔断（backfill 一直有，collect 原先完全没有）。"""
    page = FakePage(body={
        "code": 0,
        "zpData": {"jobList": [{
            "encryptJobId": "j1", "jobName": "AI Agent 工程师",
            "brandName": "示例科技", "cityName": "杭州",
            "salaryDesc": "15-25K", "jobExperience": "经验不限",
            "jobDegree": "本科", "securityId": "s1",
        }]},
    })
    collector = make_collector(page, collect_details=True)
    collector.code37_count = 99  # 已远超阈值
    collector.code37_limit = 35

    async def run():
        from job_radar.models import CityConfig, SearchFilters
        collector._goto_search = lambda *a, **k: asyncio.sleep(0)  # type: ignore[assignment]
        async for _ in collector.collect(
            CityConfig(name="杭州", boss_code="101210100"),
            "AI Agent", SearchFilters(), pages=1, detail_limit=10,
        ):
            pass

    with pytest.raises(RiskControlStop) as err:
        asyncio.run(run())
    assert "超阈值" in str(err.value)


def test_security_check_recovery_is_counted(store):
    """重算 stoken 要发两次导航，都是真实请求且消耗信用。
    旧代码只数 fetch，重算开销在账上完全隐形。"""
    page = FakePage()
    collector = make_collector(page, store)
    collector._count_request(
        f"{ORIGIN}/web/common/security-check.html?seed=x",
        outcome="code37_recover", code=37,
    )
    assert store.request_counts()["security_check"] == 1
