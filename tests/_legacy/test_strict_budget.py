"""严格日线的离线测试：原子预留、跨进程不超额、失败零 fetch、回填失败可观测且止血。

环境口径（**由测试显式提供，生产代码不做任何"是不是测试"的猜测**）：
- 每个 collector 都注入 tmp 目录里的 `JobStore`（有效账本）与 `AccessGate`（并显式
  `release()` 成 ready）+ `network_enabled=True`，否则请求路径会被闸门/账本拦住 ——
  这正是生产语义。
- 浏览器一律是替身；零 BOSS 网络；全部文件落在 `tmp_path`。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig
from job_radar.safety import AccessBlocked, AccessGate
from job_radar.storage import BudgetExceeded, JobStore, budget_scope
from tests.fakes import PageEventRecorder


@pytest.fixture(autouse=True)
def isolated_risk_dumps(tmp_path, monkeypatch):
    monkeypatch.setattr(boss_module, "RISK_DUMP_DIR", tmp_path / "risk_dumps")


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "t.sqlite3") as s:
        yield s


class FakePage(PageEventRecorder):
    """最小 Page 替身：只实现 fetch / goto / 等待需要的接口。"""

    def __init__(self, body: dict | None = None, status: int = 200):
        self.status = status
        self.body = body if body is not None else {"code": 0, "zpData": {"jobList": []}}
        self.url = ORIGIN
        self.requested_urls: list[str] = []
        self.navigations: list[str] = []

    async def evaluate(self, _script: str, arg=None) -> dict:
        if arg is not None:
            self.requested_urls.append(arg["url"] if isinstance(arg, dict) else arg)
        return {"status": self.status, "text": json.dumps(self.body, ensure_ascii=False)}

    async def goto(self, url: str, **_kw) -> None:
        self.navigations.append(url)

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    def set_default_timeout(self, _ms: int) -> None:
        return None


def ready_gate(tmp_path, name: str = "t.sqlite3") -> AccessGate:
    """显式 ready 的临时闸门（写的是 tmp 策略文件，真实 data/ 全程不被触碰）。"""
    gate = AccessGate(tmp_path / name)
    gate.release("测试显式放行：策略 ready + network_enabled=True")
    return gate


def make_collector(page: FakePage, store: JobStore | None = None, *,
                   gate: AccessGate | None = None, ledger=None,
                   network_enabled: bool = True, **overrides) -> BossCollector:
    """构造替身 collector。

    ⚠️ 闸门必须**显式**给：不给就不再有任何"是不是测试"的猜测 ——
    `_policy_gate()` 会退回 `config.database_path`（真实 data/），
    那正是生产语义（冻结期拦住一切）。所以这里默认给一个 tmp 的 ready 闸门。
    """
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        "network_enabled": network_enabled,
        **overrides,
    }
    collector = BossCollector(CollectionConfig.model_validate(payload))
    collector.page = page  # type: ignore[assignment]
    if store is not None:
        collector.ledger = store
        collector.ledger_script = "test"
    elif ledger is not None:
        collector.ledger = ledger
    if gate is not None:
        collector.gate = gate
    elif store is not None:
        collector.gate = ready_gate(store.path.parent)
    return collector


def rows(store: JobStore) -> list[sqlite3.Row]:
    return store.conn.execute(
        "SELECT id, kind, outcome, code FROM request_log ORDER BY id").fetchall()


# ---------------------------------------------------------------- 预留本身


def test_reserve_inserts_pending_row(store):
    rowid = store.reserve_request("detail", 10, script="test")
    assert rowid > 0
    (row,) = rows(store)
    assert (row["id"], row["kind"], row["outcome"], row["code"]) == (rowid, "detail", "pending", None)


def test_mark_outcome_updates_only_its_own_row(store):
    """回填必须按 rowid，不能用 MAX(id) —— 并发时最后一行可能是别人的。"""
    first = store.reserve_request("list", 10, script="a")
    store.mark_outcome(first, "ok")
    second = store.reserve_request("detail", 10, script="b")
    third = store.log_request("detail", script="other")
    assert store.mark_outcome(second, "risk", 32) is True
    got = {r["id"]: (r["outcome"], r["code"]) for r in rows(store)}
    assert got[second] == ("risk", 32)
    assert got[third] == ("ok", None), "不能改到 MAX(id) 那一行（第三方）"
    assert got[first] == ("ok", None)


def test_budget_exceeded_inserts_nothing(store):
    for _ in range(3):
        store.reserve_request("detail", 3)
    before = len(rows(store))
    with pytest.raises(BudgetExceeded) as err:
        store.reserve_request("detail", 3)
    assert "今日累计请求已达 3/3" in str(err.value)
    assert len(rows(store)) == before, "被拒绝的预留不能留下行"


def test_daily_cap_must_be_positive_or_none(store):
    for bad in (0, -1):
        with pytest.raises(ValueError):
            store.reserve_request("detail", bad)
    assert store.reserve_request("detail", None) > 0, "None = 明确不设线（离线工具）"


def test_budget_scope_separates_the_two_chains(store):
    """采集链路与 detail_page 各算各的线：一方到顶不影响另一方。"""
    assert "detail_page" not in budget_scope("list")
    assert set(budget_scope("detail")) == set(JobStore.COLLECT_KINDS)
    for _ in range(2):
        store.reserve_request("list", 2)
    with pytest.raises(BudgetExceeded):
        store.reserve_request("detail", 2)
    assert store.reserve_request("detail_page", 2) > 0
    with pytest.raises(BudgetExceeded):
        store.reserve_request("detail_page", 1)


def test_reservation_survives_reopen(tmp_path):
    path = tmp_path / "t.sqlite3"
    with JobStore(path) as a:
        a.reserve_request("detail", 2)
    with JobStore(path) as b:
        b.reserve_request("detail", 2)
        with pytest.raises(BudgetExceeded):
            b.reserve_request("detail", 2)


def test_concurrent_reservation_never_exceeds_cap(tmp_path):
    """两个线程各自开连接抢同一条线：成功数必须**恰好**等于上限。

    这是 `BEGIN IMMEDIATE` 存在的理由：先 SELECT 再 INSERT 的写法在这里会双花。
    连接在真正抢之前建好（barrier 对齐）—— 真实部署里每个进程启动时就开库；
    而 `executescript(PRAGMA journal_mode=WAL)` 在另一个写事务持锁时本来就会
    busy，用它当并发点会测出无关的假失败。
    """
    path = tmp_path / "t.sqlite3"
    cap = 5
    barrier = threading.Barrier(2, timeout=30)
    init_lock = threading.Lock()

    def worker(_n: int) -> tuple[int, int]:
        ok = rejected = 0
        with init_lock:
            store = JobStore(path)
        try:
            barrier.wait()
            for _ in range(12):
                try:
                    store.reserve_request("detail", cap)
                    ok += 1
                except BudgetExceeded:
                    rejected += 1
        finally:
            store.close()
        return ok, rejected

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sum(r[0] for r in results) == cap, f"超额了：{results}"
    assert sum(r[1] for r in results) == 24 - cap
    with JobStore(path) as s:
        assert len(rows(s)) == cap


def test_in_transaction_caller_does_not_break_reservation(store):
    store.conn.execute("INSERT INTO request_log(ts, day, kind, outcome, script, note) "
                       "VALUES ('2026-01-01 00:00:00','2026-01-01','list','ok','x','')")
    assert store.conn.in_transaction
    assert store.reserve_request("list", 10) > 0


# ---------------------------------------------------------------- 采集路径


def test_over_budget_stops_before_any_fetch(store):
    """`budget_mode=enforce`（用户显式选择）时，到线一个请求都不发。"""
    for _ in range(5):
        store.log_request("detail", script="other")
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=5,
                              budget_mode="enforce")
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "今日累计请求已达" in str(err.value)
    assert page.requested_urls == []


def test_slow_mode_cannot_bypass_the_cap(store):
    """enforce + slow：降速只影响节奏，硬线仍然硬。"""
    for _ in range(5):
        store.log_request("detail", script="other")
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=5,
                              budget_mode="enforce")
    collector.config.over_quota_action = "slow"
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "今日累计请求已达" in str(err.value)
    assert page.requested_urls == []


def test_check_daily_budget_stops_in_every_mode_when_enforcing(store):
    """enforce 下 `_check_daily_budget()` 自身也必须抛（stop/slow 都一样）。"""
    for _ in range(5):
        store.log_request("detail", script="other")
    page = FakePage()
    for mode in ("stop", "slow"):
        collector = make_collector(page, store, max_requests_per_day=5,
                                   budget_mode="enforce")
        collector.config.over_quota_action = mode
        with pytest.raises(RiskControlStop):
            collector._check_daily_budget()


# ------------------------------------------------- reference（默认）：只提示，不停止


def test_reference_mode_exceeds_line_and_keeps_recording(store, capsys):
    """默认 reference：越过参考线继续跑，且**每一条都正常记账**。"""
    for _ in range(5):
        store.log_request("detail", script="other")
    seed_ids = {r["id"] for r in rows(store)}
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=1)  # 默认 reference
    assert collector.config.budget_mode == "reference"
    for _ in range(3):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 3, "reference 模式不因越线停"
    assert collector.request_count == 3
    fresh = [r for r in rows(store) if r["id"] not in seed_ids]
    assert [r["outcome"] for r in fresh] == ["ok", "ok", "ok"], "越线后依然逐条记账"
    # 提示必须出现，且明确说"不停止"
    out = capsys.readouterr().out
    assert "[BUDGET-REFERENCE]" in out and "不停止" in out


def test_reference_mode_ignores_max_requests_per_run(store, capsys):
    """单轮参考线在 reference 下同样只提示：不打断长跑。"""
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_run=1,
                              max_requests_per_day=5000)
    for _ in range(4):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 4
    assert collector.request_count == 4
    assert "[BUDGET-REFERENCE]" in capsys.readouterr().out


def test_reference_notice_is_rate_limited(store, capsys):
    """提示要限频：跑 120 次不能刷 120 行（否则真正的风控行会被淹掉）。"""
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=1,
                              max_requests_per_run=5000)
    for _ in range(120):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    notices = [ln for ln in capsys.readouterr().out.splitlines()
               if "[BUDGET-REFERENCE]" in ln]
    assert 1 <= len(notices) <= 5, f"提示行数应为 1-5，实际 {len(notices)}"
    assert collector.request_count == 120


def test_reference_mode_passes_no_cap_to_reserve(store):
    """reference 模式必须把 daily_cap 传成 None（只记账，不设线）。"""
    seen: list = []

    class RecordingLedger:
        def __init__(self, path):
            self.path = path

        def reserve_request(self, kind, daily_cap, script=""):
            seen.append(daily_cap)
            return 11

        def request_counts(self, *_a, **_k):
            return {"total": 999}

        def mark_outcome(self, *_a, **_k):
            return True

    page = FakePage()
    collector = make_collector(page, ledger=RecordingLedger(store.path),
                               gate=ready_gate(store.path.parent))
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert seen == [None]
    assert len(page.requested_urls) == 1


def test_enforce_mode_passes_the_cap_to_reserve(store):
    """enforce 模式把参考线当硬线传下去（原子预留负责拦）。"""
    seen: list = []

    class RecordingLedger:
        def __init__(self, path):
            self.path = path

        def reserve_request(self, kind, daily_cap, script=""):
            seen.append(daily_cap)
            return 12

        def request_counts(self, *_a, **_k):
            return {"total": 0}

        def mark_outcome(self, *_a, **_k):
            return True

    page = FakePage()
    collector = make_collector(page, ledger=RecordingLedger(store.path),
                               gate=ready_gate(store.path.parent),
                               max_requests_per_day=7, budget_mode="enforce")
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert seen == [7]


def test_reference_mode_still_stops_when_ledger_is_broken(store):
    """reference 只让"参考线"变软：可观测性故障在任何模式下都硬停。"""
    page = FakePage()

    class BrokenLedger:
        path = store.path

        def reserve_request(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        def request_counts(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

    collector = make_collector(page, ledger=BrokenLedger(),
                               gate=ready_gate(store.path.parent))
    assert collector.config.budget_mode == "reference"
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "账本不可用" in str(err.value)
    assert page.requested_urls == []


def test_reference_mode_still_stops_on_real_risk_response(store):
    """reference 不影响风控：真实风险码照旧持久熔断 + 停机。"""
    gate = ready_gate(store.path.parent)
    page = FakePage(body={"code": 32, "message": "您的账户已暂时被禁止使用"})
    collector = make_collector(page, store, gate=gate, max_requests_per_day=1)
    assert collector.config.budget_mode == "reference"
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    assert gate.read_state().mode == "paused"
    assert len(page.requested_urls) == 1


def test_reference_mode_still_stops_on_freeze(store):
    """reference 不影响持久冻结：paused 策略照旧在请求之前拦住。"""
    paused = AccessGate(store.path)
    paused.pause("人工冻结中")
    page = FakePage()
    collector = make_collector(page, store, gate=paused)
    with pytest.raises(AccessBlocked):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.requested_urls == []


def test_check_daily_budget_stops_without_ledger_or_on_read_failure(store):
    """账本缺失 / 账本读失败：一律停（这两种情况下"发了多少"没有答案）。"""
    page = FakePage()
    bare = make_collector(page)                     # 没有账本
    with pytest.raises(RiskControlStop) as err:
        bare._check_daily_budget()
    assert "没有注入请求账本" in str(err.value)

    class BrokenCounts:
        path = store.path

        def reserve_request(self, *_a, **_k):
            raise AssertionError("不该走到预留")

        def request_counts(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

    broken = make_collector(page, ledger=BrokenCounts(), gate=ready_gate(store.path.parent))
    with pytest.raises(RiskControlStop) as err:
        broken._check_daily_budget()
    assert "账本不可用" in str(err.value)


def test_reserve_refuses_missing_ledger_and_legacy_ledger(store):
    """`_reserve_request` 没有兼容退路：缺账本 / 缺 reserve_request 都要停。

    旧实现会给"只实现 log_request 的账本"退回落账语义，等于谁换个账本就能
    绕过额度判断 —— 这条钉死那个退路不存在。
    """
    page = FakePage()
    bare = make_collector(page)
    with pytest.raises(RiskControlStop) as err:
        bare._reserve_request("/wapi/zpgeek/search/joblist.json")
    assert "没有注入请求账本" in str(err.value)

    calls: list[str] = []

    class LegacyLedger:
        path = store.path

        def log_request(self, kind, **_kw):
            calls.append(kind)
            return 1

        def request_counts(self, *_a, **_k):
            return {"total": 0}

    legacy = make_collector(page, ledger=LegacyLedger(),
                            gate=ready_gate(store.path.parent))
    with pytest.raises(RiskControlStop) as err:
        legacy._reserve_request("/wapi/zpgeek/search/joblist.json")
    assert "不支持原子预留" in str(err.value)
    assert calls == [], "不得退回 log_request 记账后继续"


def test_missing_ledger_never_reaches_the_network(store):
    """端到端：没有账本时 `_same_origin_fetch` 一个请求都不发。"""
    page = FakePage()
    collector = make_collector(page, gate=ready_gate(store.path.parent))
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.requested_urls == []


def test_legacy_ledger_never_reaches_the_network(store):
    class LegacyLedger:
        path = store.path

        def log_request(self, *_a, **_k):
            raise AssertionError("不得退回 log_request")

        def request_counts(self, *_a, **_k):
            return {"total": 0}

    page = FakePage()
    collector = make_collector(page, ledger=LegacyLedger(),
                               gate=ready_gate(store.path.parent))
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.requested_urls == []


def test_every_fetch_checks_gate_and_switch_without_start(tmp_path, store):
    """没有 start() 也要查闸门：直接注入 page 的调用可能是真实脚本，不是替身。"""
    page = FakePage()
    paused = AccessGate(store.path)
    paused.pause("冻结中")
    collector = make_collector(page, store, gate=paused)
    with pytest.raises(AccessBlocked):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.requested_urls == []

    # 策略 ready 但 network_enabled=False：同样不放行
    off = make_collector(page, store, gate=ready_gate(store.path.parent),
                         network_enabled=False)
    with pytest.raises(AccessBlocked):
        asyncio.run(off._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.requested_urls == []


def test_goto_search_checks_gate_before_navigating(store):
    """导航本身也是真实请求：`_goto_search` 在 goto 之前就要被拦住。"""
    page = FakePage()
    paused = AccessGate(store.path)
    paused.pause("冻结中")
    collector = make_collector(page, store, gate=paused)
    with pytest.raises(AccessBlocked):
        asyncio.run(collector._goto_search(f"{ORIGIN}/web/geek/jobs"))
    assert page.navigations == []

    off = make_collector(page, store, gate=ready_gate(store.path.parent),
                         network_enabled=False)
    with pytest.raises(AccessBlocked):
        asyncio.run(off._goto_search(f"{ORIGIN}/web/geek/jobs"))
    assert page.navigations == []


def test_under_budget_reserves_then_marks_ok(store):
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=10)
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1
    (row,) = rows(store)
    assert (row["kind"], row["outcome"]) == ("list", "ok")
    assert row["outcome"] != "pending", "响应拿到后必须回填结论"


def test_each_retry_reserves_its_own_row(store):
    """重试也是真实尝试：每次都要预留（否则账本低估、闸门偏松）。"""

    class FlakyPage(FakePage):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def evaluate(self, script, arg=None) -> dict:
            if arg is not None:
                self.attempts += 1
                self.requested_urls.append(arg["url"])
                if self.attempts == 1:
                    return {"status": -1, "text": "", "error": "TypeError: Failed to fetch"}
            return await super().evaluate(script, arg)

    page = FlakyPage()
    collector = make_collector(page, store, max_requests_per_day=10)
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert page.attempts == 2
    got = [(r["outcome"], r["kind"]) for r in rows(store)]
    assert got == [("retry", "list"), ("ok", "list")]


def test_ledger_failure_stops_before_any_fetch(store):
    """账本不可用 = 无法确认额度 → 停，且不发请求（fail-closed）。"""
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=10)
    store.close()
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "账本不可用" in str(err.value)
    assert page.requested_urls == []


# ---------------------------------------------------------------- 回填失败：可观测 + 止血


class NoMarkLedger:
    """有 reserve_request，但没有 mark_outcome（回填无处可写）。"""

    def __init__(self, path):
        self.path = path
        self.marked: list = []

    def reserve_request(self, *_a, **_k):
        return 7

    def request_counts(self, *_a, **_k):
        return {"total": 0}


def test_mark_failure_is_visible_and_stops_next_request(store, capsys):
    """回填失败不能静默：日志可见，且**下一次请求前**停止。"""
    page = FakePage()
    collector = make_collector(page, ledger=NoMarkLedger(store.path),
                               gate=ready_gate(store.path.parent))
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1, "第一次请求本身是允许的（额度已预留）"
    out = capsys.readouterr().out
    assert "[LEDGER-FAIL]" in out, "回填失败必须打出可见的失败信号"
    assert collector._ledger_failures == 1

    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "账本回填失败" in str(err.value)
    assert len(page.requested_urls) == 1, "止血后不得再发请求"


def test_mark_exception_is_visible_and_stops_next_request(store, capsys):
    """mark_outcome 抛异常同样可观测 + 止血。"""

    class ExplodingMark(NoMarkLedger):
        def mark_outcome(self, *_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

    page = FakePage()
    collector = make_collector(page, ledger=ExplodingMark(store.path),
                               gate=ready_gate(store.path.parent))
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert "[LEDGER-FAIL]" in capsys.readouterr().out
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))


def test_risk_pause_survives_mark_failure(store, capsys):
    """风控分支：回填失败**不得**先抛出去把持久熔断丢掉。

    顺序必须是"先写策略，再决定怎么抛"；本实现让回填失败只记日志/计数（从不抛），
    所以 `gate.pause()` 一定会执行。
    """
    ledger = NoMarkLedger(store.path)
    gate = ready_gate(store.path.parent)
    page = FakePage(body={"code": 32, "message": "您的账户已暂时被禁止使用"})
    collector = make_collector(page, ledger=ledger, gate=gate)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    assert "[LEDGER-FAIL]" in capsys.readouterr().out
    assert gate.read_state().mode == "paused", "回填失败不得阻止持久熔断落地"


def test_risk_marks_its_own_reserved_row(store):
    """风控结论落在**本次请求预留的那一行**，而不是最后一行。"""
    decoy = store.reserve_request("list", 100, script="other_process")
    page = FakePage(body={"code": 32, "message": "您的账户已暂时被禁止使用"})
    collector = make_collector(page, store, max_requests_per_day=100)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    got = {r["id"]: (r["outcome"], r["code"]) for r in rows(store)}
    assert got[decoy] == ("pending", None), "别人预留的行不能被改判"
    risk_rows = [r for r in rows(store) if r["outcome"] == "risk"]
    assert len(risk_rows) == 1 and risk_rows[0]["code"] == 32
    assert risk_rows[0]["id"] > decoy


def test_throttle_message_is_recorded_as_throttled(store):
    page = FakePage(body={"code": 0, "message": "访问频繁，请稍后再试"})
    collector = make_collector(page, store, max_requests_per_day=100)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert [r["outcome"] for r in rows(store)] == ["throttled"]


def test_budget_stop_message_does_not_claim_unverified_mechanism(store):
    """文案不得宣称"封禁按累计量算"这类未经验证的机制，也不得把它说成平台额度。"""
    for _ in range(2):
        store.log_request("detail", script="other")
    page = FakePage()
    collector = make_collector(page, store, max_requests_per_day=2,
                              budget_mode="enforce")
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    msg = str(err.value)
    assert "封禁按累计量算" not in msg
    assert "自设" in msg and "不是平台给的额度" in msg
    assert "enforce" in msg


def test_start_requires_valid_ledger_and_budget(tmp_path, store):
    """start() 在起浏览器之前确认账本与预算；不满足就不许起浏览器。

    三条分别是：没账本、账本不支持原子预留、enforce 下已越线。
    对照的一条：reference（默认）越线时**照常启动**（只提示）。
    """
    page = FakePage()

    class BlockingSession:
        """start() 一旦被调用就断言失败：这些用例都应该在它之前就停。"""

        def __init__(self):
            self.started = False

        async def start(self):
            self.started = True
            raise AssertionError("不该走到浏览器启动")

        async def close(self):
            return None

    class WorkingSession:
        """reference 用例用：真的走完 start()。"""

        def __init__(self):
            self.started = False

        async def start(self):
            self.started = True
            outer = self

            class Ctx:
                pages: list = []

                async def new_page(self):
                    return FakePage()

            return Ctx()

        async def close(self):
            return None

    def with_session(collector, session):
        collector.browser_session = session  # type: ignore[assignment]
        return collector

    # ① 没有账本
    bare = with_session(make_collector(page, gate=ready_gate(tmp_path)),
                        BlockingSession())
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(bare.start())
    assert "没有注入请求账本" in str(err.value)
    assert bare.browser_session.started is False  # type: ignore[attr-defined]

    # ② 账本不支持原子预留（只有 log_request 的旧式账本）
    class NoReserveLedger:
        def __init__(self, path):
            self.path = path

        def log_request(self, *_a, **_k):
            raise AssertionError("不得退回 log_request 记账后继续")

        def request_counts(self, *_a, **_k):
            return {"total": 0}

    legacy = with_session(make_collector(
        page, ledger=NoReserveLedger(store.path), gate=ready_gate(tmp_path)),
        BlockingSession())
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(legacy.start())
    assert "不支持原子预留" in str(err.value)
    assert legacy.browser_session.started is False  # type: ignore[attr-defined]

    # ③ enforce 下预算已到顶
    for _ in range(5):
        store.log_request("detail", script="other")
    capped = with_session(make_collector(page, store, max_requests_per_day=5,
                                         budget_mode="enforce"),
                          BlockingSession())
    with pytest.raises(RiskControlStop) as err:
        asyncio.run(capped.start())
    assert "今日累计请求已达" in str(err.value)
    assert capped.browser_session.started is False  # type: ignore[attr-defined]

    # ④ reference（默认）越线不拦启动：只提示，照常起浏览器
    reference_session = WorkingSession()
    reference = with_session(make_collector(page, store, max_requests_per_day=5),
                             reference_session)
    assert reference.config.budget_mode == "reference"
    asyncio.run(reference.start())
    assert reference_session.started is True


def test_start_blocks_without_gate_or_switch(tmp_path, store):
    """策略没 ready / 开关没开：同样在浏览器之前停。"""
    page = FakePage()

    class FakeSession:
        def __init__(self):
            self.started = False

        async def start(self):
            self.started = True
            raise AssertionError("不该走到浏览器启动")

        async def close(self):
            return None

    paused = AccessGate(store.path)
    paused.pause("冻结中")
    c1 = make_collector(page, store, gate=paused, max_requests_per_day=100)
    c1.browser_session = FakeSession()  # type: ignore[assignment]
    with pytest.raises(AccessBlocked):
        asyncio.run(c1.start())
    assert c1.browser_session.started is False  # type: ignore[attr-defined]

    c2 = make_collector(page, store, gate=ready_gate(store.path.parent),
                        network_enabled=False, max_requests_per_day=100)
    c2.browser_session = FakeSession()  # type: ignore[assignment]
    with pytest.raises(AccessBlocked):
        asyncio.run(c2.start())
    assert c2.browser_session.started is False  # type: ignore[attr-defined]


def test_thread_safety_of_the_shared_store_is_not_required():
    """说明性用例：JobStore 是单线程连接（sqlite3 默认），跨线程要各开一个。

    真正的并发测试是上面那个 ThreadPoolExecutor 的例子。
    """
    assert threading.current_thread() is threading.main_thread()
