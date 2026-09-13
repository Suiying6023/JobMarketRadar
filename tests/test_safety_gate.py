"""访问策略闸门的离线测试：默认拒绝、持久冻结、过期不放行、损坏即拒绝。

全部隔离在 `tmp_path`：不读真实 `data/`、不连 BOSS、不起浏览器。
采集侧的替身只实现到 `start()` / `_same_origin_fetch()` 需要的接口。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig
from job_radar.safety import (
    MODE_OFFLINE,
    MODE_PAUSED,
    MODE_READY,
    AccessBlocked,
    AccessGate,
    release_blocked_reason,
)


@pytest.fixture(autouse=True)
def isolated_risk_dumps(tmp_path, monkeypatch):
    """风控正文落盘必须重定向：否则假响应会写进真实取证目录。"""
    monkeypatch.setattr(boss_module, "RISK_DUMP_DIR", tmp_path / "risk_dumps")


def gate_in(tmp_path) -> AccessGate:
    """状态文件在临时目录里 —— 真实 `data/access-policy.json` 全程不被触碰。"""
    return AccessGate(tmp_path / "t.sqlite3")


# ---------------------------------------------------------------- 策略本身


def test_missing_state_file_is_offline_and_blocks(tmp_path):
    """没有策略文件 = offline。**默认拒绝**，不是默认放行。"""
    gate = gate_in(tmp_path)
    state = gate.read_state()
    assert state.mode == MODE_OFFLINE
    assert state.manual_release_required is True
    with pytest.raises(AccessBlocked) as err:
        gate.require_online(True)
    assert "access-policy.json" in str(err.value)


def test_disabled_never_reads_policy(tmp_path):
    """`enabled=False` 时即使策略是 ready 也不放行（必须显式打开开关）。"""
    gate = gate_in(tmp_path)
    gate.release("测试：先写一个 ready 策略")
    with pytest.raises(AccessBlocked):
        gate.require_online(False)
    assert gate.require_online(True).mode == MODE_READY


def test_ready_state_allows(tmp_path):
    gate = gate_in(tmp_path)
    gate.release("人工确认：账号已自然解封")
    state = gate.require_online(True)
    assert state.mode == MODE_READY
    assert state.manual_release_required is False


def test_pause_is_persisted_across_gate_instances(tmp_path):
    """冻结要活过进程：换一个 AccessGate（= 换一个进程）读到的仍是 paused。"""
    path = tmp_path / "t.sqlite3"
    AccessGate(path).pause("命中 code=32", source="test")
    fresh = AccessGate(path)          # 模拟重启后新建的闸门
    state = fresh.read_state()
    assert state.mode == MODE_PAUSED
    assert state.source == "test"
    assert "code=32" in state.reason
    with pytest.raises(AccessBlocked):
        fresh.require_online(True)


def test_pause_without_reason_still_records_placeholder(tmp_path):
    gate = gate_in(tmp_path)
    state = gate.pause("   ")
    assert state.mode == MODE_PAUSED
    assert state.reason == "(未记录原因)"


def test_expired_resume_after_does_not_auto_release(tmp_path):
    """到期**不等于**放行：状态仍是 paused，必须人工 release。"""
    gate = gate_in(tmp_path)
    past = datetime.now().astimezone() - timedelta(minutes=5)
    gate.pause("封禁 3 天", resume_after=past.isoformat(timespec="seconds"))
    assert gate.read_state().mode == MODE_PAUSED
    with pytest.raises(AccessBlocked):
        gate.require_online(True)
    # 但此时人工可以解除（期限已过）
    assert release_blocked_reason(gate.read_state().resume_after) == ""
    assert gate.release("解封时间已过，人工确认").mode == MODE_READY


def test_release_refused_before_deadline(tmp_path):
    """非空 resume_after 期限之前，release 必须被拒绝。"""
    gate = gate_in(tmp_path)
    future = datetime.now().astimezone() + timedelta(days=3)
    gate.pause("账号级封禁", resume_after=future.isoformat(timespec="seconds"))
    with pytest.raises(AccessBlocked) as err:
        gate.release("提前解除")
    assert "拒绝解除" in str(err.value)
    assert gate.read_state().mode == MODE_PAUSED, "被拒绝的 release 不能改状态"


def test_release_requires_non_empty_confirmation(tmp_path):
    gate = gate_in(tmp_path)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            gate.release(bad)  # type: ignore[arg-type]
    assert gate.read_state().mode == MODE_OFFLINE


def test_unparseable_resume_after_is_rejected(tmp_path):
    """把期限写成不可解析的字符串必须报错，不能当"没有期限"。"""
    gate = gate_in(tmp_path)
    with pytest.raises(ValueError):
        gate.pause("封禁", resume_after="明天下午")


@pytest.mark.parametrize("payload", [
    "{ not json",                                            # 坏 JSON
    '["list"]',                                              # 顶层不是对象
    '{"mode": "unknown-mode"}',                              # 非法 mode
    '{"mode": "paused", "reason": 5}',                       # 类型错
    '{"mode": "paused", "resume_after": "不是时间"}',        # 期限不可解析
    '{"mode": "ready", "manual_release_required": "yes"}',   # 类型错
])
def test_corrupt_state_is_rejected(tmp_path, payload):
    """损坏的策略文件一律按 offline 处理并拒绝访问（绝不猜成 ready）。"""
    gate = gate_in(tmp_path)
    gate.state_path.write_text(payload, encoding="utf-8")
    state = gate.read_state()
    assert state.mode == MODE_OFFLINE
    assert state.corrupt is True
    with pytest.raises(AccessBlocked):
        gate.require_online(True)


def test_minimal_paused_file_is_valid_and_blocks(tmp_path):
    """缺可选字段的 paused 文件仍然合法（前向兼容），但依然拦住访问。"""
    gate = gate_in(tmp_path)
    gate.state_path.write_text('{"mode": "paused"}', encoding="utf-8")
    state = gate.read_state()
    assert state.mode == MODE_PAUSED and state.corrupt is False
    with pytest.raises(AccessBlocked):
        gate.require_online(True)


def test_state_file_roundtrip_leaves_no_temp_files(tmp_path):
    """原子写：写完只剩策略文件（与它的锁文件），临时文件必须已被 replace 掉。"""
    gate = gate_in(tmp_path)
    gate.pause("风控 A", resume_after=(datetime.now().astimezone()
                                       + timedelta(hours=1)).isoformat(timespec="seconds"))
    names = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert "access-policy.json" in names
    assert not [n for n in names if n.endswith(".tmp")], f"残留临时文件: {names}"
    assert set(names) <= {"access-policy.json", "access-policy.json.lock"}
    data = json.loads(gate.state_path.read_text(encoding="utf-8"))
    assert data["mode"] == MODE_PAUSED
    assert data["manual_release_required"] is True
    assert data["resume_after"], "期限要落进文件，否则重启后就等于没有期限"


def test_explain_mentions_next_step(tmp_path):
    gate = gate_in(tmp_path)
    gate.pause("命中平台风控 HTTP 200 code=32")
    text = gate.explain()
    assert "paused" in text and "release" in text and "access-policy.json" in text


# ------------------------------------------------- 与 release 的锁竞争（local_files）


@pytest.fixture
def fast_lock(monkeypatch):
    """把锁重试预算压到毫秒级：断言的是"拒绝"，不是"等 5 秒"。"""
    from job_radar import safety
    monkeypatch.setattr(safety, "_LOCK_TRIES", 3)
    monkeypatch.setattr(safety, "_LOCK_SLEEP", 0.01)


def test_pause_refuses_when_lock_is_held(tmp_path, fast_lock):
    """抢不到策略锁 → **拒绝写**，绝不无锁写。

    无锁写的危害：`release()` 持锁期间读到旧的 paused，写出 ready 覆盖掉
    刚落地的冻结 —— 冻结静默丢失。所以这里的正确行为是抛错 + 状态文件一字不改。
    """
    from job_radar.local_files import file_mutex
    from job_radar.safety import AccessPolicyLocked

    gate = gate_in(tmp_path)
    gate.release("先放开，便于观察被拒绝的 pause 有没有写")
    before = gate.state_path.read_bytes()
    lock_path = gate.state_path.with_name(gate.state_path.name + ".lock")
    with file_mutex(lock_path):
        with pytest.raises(AccessPolicyLocked) as err:
            gate.pause("风控响应，但锁被占用")
        assert "拒绝无锁写" in str(err.value)
    assert gate.state_path.read_bytes() == before, "被拒绝的 pause 不能写文件"
    assert gate.read_state().mode == MODE_READY


def test_pause_keeps_later_deadline(tmp_path):
    """第二条风险不得抹掉已知的更晚解封期限（release 的期限校验靠它）。"""
    gate = gate_in(tmp_path)
    day1 = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
    gate.pause("封禁 1 天", resume_after=day1)
    # 只写 reason 的第二次 pause：期限必须保留
    assert gate.pause("又撞了一次风控").resume_after == day1
    # 更早的期限不许把已知的更晚期限改早
    earlier = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="seconds")
    assert gate.pause("第三次", resume_after=earlier).resume_after == day1
    # 更晚的期限要生效
    day5 = (datetime.now().astimezone() + timedelta(days=5)).isoformat(timespec="seconds")
    assert gate.pause("第四次", resume_after=day5).resume_after == day5
    with pytest.raises(AccessBlocked):
        gate.release("提前解除")


def test_pause_keeps_existing_reason_when_blank(tmp_path):
    """只补期限、不写原因的第二次 pause：沿用上一条原因（审计不断链）。"""
    gate = gate_in(tmp_path)
    gate.pause("命中 code=32，账号级封禁")
    assert "code=32" in gate.pause("").reason


def test_release_refuses_while_lock_is_held(tmp_path, fast_lock):
    """人工解除撞上锁时**拒绝**，而不是在不知道对方写了什么的情况下覆盖。"""
    from job_radar.local_files import file_mutex
    from job_radar.safety import AccessPolicyLocked

    gate = gate_in(tmp_path)
    gate.pause("先冻结")
    before = gate.state_path.read_bytes()
    lock_path = gate.state_path.with_name(gate.state_path.name + ".lock")
    with file_mutex(lock_path):
        with pytest.raises(AccessPolicyLocked):
            gate.release("人工解除")
    assert gate.state_path.read_bytes() == before, "被拒绝的 release 不能改状态"
    assert gate.read_state().mode == MODE_PAUSED
    assert gate.release("锁已释放，人工解除").mode == MODE_READY


@pytest.mark.parametrize("payload,why", [
    ('{"mode": "ready", "manual_release_required": true}', "矛盾的 ready：仍要求人工解除"),
    ('{"mode": "ready", "resume_after": "2099-01-01T00:00:00+08:00"}', "矛盾的 ready：带期限"),
    ('{"mode": "paused", "manual_release_required": false}', "矛盾的 paused：不要求解除"),
])
def test_contradictory_state_is_corrupt_and_blocks(tmp_path, payload, why):
    """自相矛盾的状态不得被当作放行依据（require_online 只认 mode=ready）。"""
    gate = gate_in(tmp_path)
    gate.state_path.write_text(payload, encoding="utf-8")
    state = gate.read_state()
    assert state.mode == MODE_OFFLINE and state.corrupt is True, why
    with pytest.raises(AccessBlocked):
        gate.require_online(True)


@pytest.mark.parametrize("raw", [
    "NaN", "-Infinity", "Infinity",   # JSON 允许裸 NaN/Infinity，json.loads 会吃进去
    "1e309", "1e20", "10e30", "-1e20",  # 溢出：远超可表示范围
])
def test_non_finite_or_overflowing_deadline_is_corrupt(tmp_path, raw):
    """NaN/inf/溢出的数字时间戳不是"没有期限"，而是损坏 → 拒绝访问。"""
    gate = gate_in(tmp_path)
    gate.state_path.write_text(
        '{"mode": "paused", "resume_after": %s}' % raw, encoding="utf-8")
    state = gate.read_state()
    assert state.corrupt is True, f"resume_after={raw} 应判为损坏"
    assert state.mode == MODE_OFFLINE
    with pytest.raises(AccessBlocked):
        gate.require_online(True)


def test_numeric_all_ok_and_millis_are_accepted(tmp_path):
    """合法数字时间戳仍要能用（含平台风格的毫秒），别把好文件判成损坏。"""
    gate = gate_in(tmp_path)
    future_ms = int((datetime.now().astimezone()
                     + timedelta(days=2)).timestamp() * 1000)
    gate.state_path.write_text(
        '{"mode": "paused", "resume_after": %d}' % future_ms, encoding="utf-8")
    state = gate.read_state()
    assert state.corrupt is False and state.mode == MODE_PAUSED
    assert state.resume_after
    with pytest.raises(AccessBlocked):
        gate.release("还没到点")


# ---------------------------------------------------------------- 采集侧替身


class FakePage:
    def __init__(self, status: int = 200, body: dict | None = None,
                 raw_text: str | None = None):
        self.status = status
        self.body = body if body is not None else {"code": 0, "zpData": {"jobList": []}}
        self.raw_text = raw_text
        self.url = ORIGIN
        self.requested_urls: list[str] = []

    async def evaluate(self, _script: str, arg=None) -> dict:
        if arg is not None:
            self.requested_urls.append(arg["url"] if isinstance(arg, dict) else arg)
        text = (self.raw_text if self.raw_text is not None
                else json.dumps(self.body, ensure_ascii=False))
        return {"status": self.status, "text": text}

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    def set_default_timeout(self, _ms: int) -> None:
        return None


class FakeContext:
    def __init__(self, page: FakePage):
        self.pages: list = []
        self._page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class FakeBrowserSession:
    """只实现 `start()/close()`；用来断言 start() 是否真的被调用。"""

    def __init__(self, page: FakePage):
        self.page = page
        self.started = False
        self.start_calls = 0
        self.closed = False

    async def start(self) -> FakeContext:
        self.started = True
        self.start_calls += 1
        return FakeContext(self.page)

    async def close(self) -> None:
        self.closed = True


def make_collector(page: FakePage, gate: AccessGate, **overrides) -> BossCollector:
    from job_radar.storage import JobStore
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        "database_path": str(gate.state_path.parent / "test.sqlite3"),
        **overrides,
    }
    collector = BossCollector(CollectionConfig.model_validate(payload))
    collector.page = page
    collector.gate = gate
    collector.ledger = JobStore(":memory:")
    collector.browser_session = FakeBrowserSession(page)  # type: ignore[assignment]
    return collector


# ---------------------------------------------------------------- start() 闸门


def test_start_blocks_when_network_disabled(tmp_path):
    """`network_enabled=False`（新默认）：连浏览器都不许起。"""
    gate = gate_in(tmp_path)
    gate.release("策略 ready，但开关没开")
    page = FakePage()
    collector = make_collector(page, gate)
    assert collector.config.network_enabled is False
    with pytest.raises(AccessBlocked):
        asyncio.run(collector.start())
    assert collector.browser_session.started is False  # type: ignore[attr-defined]


def test_start_blocks_when_policy_paused(tmp_path):
    """开关打开但策略是 paused：同样在起浏览器之前被拦住。"""
    gate = gate_in(tmp_path)
    gate.pause("账号冻结中")
    page = FakePage()
    collector = make_collector(page, gate, network_enabled=True)
    with pytest.raises(AccessBlocked):
        asyncio.run(collector.start())
    assert collector.browser_session.started is False  # type: ignore[attr-defined]


def test_start_proceeds_when_ready_and_enabled(tmp_path):
    gate = gate_in(tmp_path)
    gate.release("人工确认可以恢复")
    page = FakePage()
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    assert collector.browser_session.started is True  # type: ignore[attr-defined]
    assert collector._gate_armed is True


def test_armed_fetch_rechecks_policy(tmp_path):
    """长跑期间策略被改成 paused：下一次 fetch 当场停，且不发请求。"""
    gate = gate_in(tmp_path)
    gate.release("开始运行")
    page = FakePage()
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1
    gate.pause("巡检发现风控，人工熔断")      # 另一个进程/人手改的
    with pytest.raises(AccessBlocked):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json"))
    assert len(page.requested_urls) == 1, "被拦住后不能再发第二次请求"


# ---------------------------------------------------------------- 风险 → 持久熔断


def test_risk_response_pauses_policy_and_stops(tmp_path):
    gate = gate_in(tmp_path)
    gate.release("开始运行")
    page = FakePage(body={"code": 32, "message": "您的账户存在异常行为，已暂时被禁止使用."})
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    state = gate.read_state()
    assert state.mode == MODE_PAUSED, "风控必须持久熔断（重启也拦得住）"
    assert "32" in state.reason
    with pytest.raises(AccessBlocked):
        gate.require_online(True)


def test_risk_with_unwritable_policy_still_stops_and_says_so(tmp_path, fast_lock):
    """策略写不进去（锁被占用）时：**照停，但明说没持久化**，且不无锁写。

    这条钉的是"不能把'没写进去'伪装成'已熔断'"：调用方必须能从异常消息里
    看出冻结没落地，从而知道重启后仍需人工确认。
    """
    from job_radar.local_files import file_mutex

    gate = gate_in(tmp_path)
    gate.release("开始运行")
    before = gate.state_path.read_bytes()
    page = FakePage(body={"code": 32, "message": "您的账户存在异常行为"})
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    lock_path = gate.state_path.with_name(gate.state_path.name + ".lock")
    with file_mutex(lock_path):
        with pytest.raises(RiskControlStop) as err:
            asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    assert "SAFETY-FAIL" in str(err.value), "异常消息必须点明策略未写入"
    assert gate.state_path.read_bytes() == before, "锁被占用时不得无锁写策略"
    assert len(page.requested_urls) == 1


def test_html_challenge_pauses_policy(tmp_path):
    gate = gate_in(tmp_path)
    gate.release("开始运行")
    page = FakePage(raw_text="<html><script>请完成滑块验证</script></html>")
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))
    assert gate.read_state().mode == MODE_PAUSED


def test_code37_is_risk_first_and_never_recovers(tmp_path):
    """code=37 必须先按风控处理：不重算 stoken、不重试、直接熔断。

    回归的是事故形态：旧实现把「环境存在异常 → 重算 → 重试」放在最前面，
    于是在已经触发风控的会话上又补发导航。
    """
    gate = gate_in(tmp_path)
    gate.release("开始运行")
    page = FakePage(body={"code": 37, "message": "您的环境存在异常.",
                          "zpData": {"seed": "s", "name": "n", "ts": "1"}})
    collector = make_collector(page, gate, network_enabled=True)
    asyncio.run(collector.start())
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json",
                                                 recover_code37=True))
    assert len(page.requested_urls) == 1, "不得为重算/重试再发第二次请求"
    assert gate.read_state().mode == MODE_PAUSED


def test_recover_code37_direct_call_is_blocked(tmp_path):
    """直接调用 `_recover_code37` 也不得绕开（否则等于留了一条隐路径）。"""
    gate = gate_in(tmp_path)
    gate.release("开始运行")
    page = FakePage()
    collector = make_collector(page, gate)
    collector.page = page
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._recover_code37("seed", "name", "ts"))
    assert page.requested_urls == []
    assert gate.read_state().mode == MODE_PAUSED


def test_no_gate_no_ledger_never_guesses_policy_path(tmp_path):
    """没注入 gate、也没有账本时，**不许猜**策略目录：照样停机但不落盘。

    猜错目录的后果是把冻结写到别的库上（离线测试最典型：会把 paused 写进真实
    `data/access-policy.json`，让真环境凭空被拦住）。
    """
    page = FakePage(body={"code": 36, "message": "您的账户存在异常行为"})
    collector = make_collector(page, gate_in(tmp_path))
    collector.gate = None
    collector.ledger = None
    assert collector._pause_gate() is None, "认不出库目录时不得自行构造可写闸门"
    with pytest.raises(AccessBlocked):
        asyncio.run(collector._same_origin_fetch("/x"))
    # 读路径仍可用（start/每次 fetch 的复查需要一个能读的闸门）
    assert collector._policy_gate() is not None
