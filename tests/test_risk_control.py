from __future__ import annotations

import asyncio
import json

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    """把重试退避的真实等待打掉。

    退避时长是被测行为（固定间隔重试会在对端持续拒绝时每 2s 重建一轮），
    但**让测试真的等**只是把套件从 5s 拖到 15s。这里只跳过挂钟时间，
    重试次数与顺序仍然被断言覆盖。
    """
    async def instant(_seconds):
        return None

    monkeypatch.setattr(boss_module.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def risk_dump_dir(tmp_path, monkeypatch):
    """把风控落盘目录重定向到临时目录。

    否则每跑一次测试就往真实的 data/risk_dumps/ 写一份假的「请完成滑块验证」，
    把真实风控取证污染掉——实测已积累 15 份测试产生的假 dump，看上去像当天
    真的被风控了，会误导后续对账号状态的判断。autouse 覆盖本模块所有用例。
    """
    target = tmp_path / "risk_dumps"
    monkeypatch.setattr(boss_module, "RISK_DUMP_DIR", target)
    return target


class FakePage:
    """最小的 Page 替身，只实现 _same_origin_fetch 用到的接口。"""

    def __init__(
        self,
        status: int = 200,
        body: dict | None = None,
        url: str = ORIGIN,
        raw_text: str | None = None,
    ):
        self.status = status
        self.body = body if body is not None else {"code": 0, "zpData": {"jobList": []}}
        self.url = url
        self.raw_text = raw_text
        self.requested_urls: list[str] = []

    async def evaluate(self, _script: str, arg=None) -> dict:
        # _read_token 传 None，fetch 传 {"url":..., "token":...}。只有 fetch 记录 URL。
        if arg is not None:
            url = arg["url"] if isinstance(arg, dict) else arg
            self.requested_urls.append(url)
        text = self.raw_text if self.raw_text is not None else json.dumps(self.body, ensure_ascii=False)
        return {"status": self.status, "text": text}

    async def wait_for_timeout(self, _ms: int) -> None:
        return None


def make_collector(page: FakePage, **overrides) -> BossCollector:
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        **overrides,
    }
    collector = BossCollector(CollectionConfig.model_validate(payload))
    collector.page = page  # type: ignore[assignment]
    return collector


def test_fetch_uses_absolute_url():
    """页面可能停在 about:blank，相对路径会解析失败，所以必须发绝对地址。"""
    page = FakePage()
    collector = make_collector(page)
    asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/search/joblist.json?page=1"))
    assert page.requested_urls == [f"{ORIGIN}/wapi/zpgeek/search/joblist.json?page=1"]


@pytest.mark.parametrize("code", [31, 36, 37])
def test_risk_codes_stop_run(code: int):
    """36 是实测遇到的「您的账户存在异常行为」，必须和 31/37 一样立即停机。"""
    page = FakePage(body={"code": code, "message": "您的账户存在异常行为"})
    collector = make_collector(page)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))


def test_risk_keyword_in_body_stops_run():
    page = FakePage(body={"code": 0, "message": "访问频繁，请稍后再试"})
    collector = make_collector(page)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))


def test_jd_containing_risk_keyword_is_not_a_risk_stop():
    """回归：风险词只查 message 字段，不扫 JSON 全文——爬虫/安全岗 JD 会提「滑块」「验证码」，
    扫全文会把好数据误判成风控（2026-08-08 实测 python电商开发 详情因此被丢弃）。"""
    page = FakePage(
        body={
            "code": 0,
            "message": "Success",
            "zpData": {"jobName": "python电商开发", "postDescription": "能解决复杂验证码（图片/滑块）、账号限制"},
        }
    )
    collector = make_collector(page)
    body = asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    assert body["code"] == 0


def test_html_challenge_page_stops_run(risk_dump_dir):
    """验证页是 HTML 不是 JSON，含滑块/验证码特征必须按风控停机。"""
    page = FakePage(raw_text="<html><body><script>请完成滑块验证</script></body></html>")
    collector = make_collector(page)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))
    # 顺带确认 dump 真的写到了隔离目录（风控取证能力本身也是被测行为）
    assert list(risk_dump_dir.glob("*.txt"))


def test_http_429_stops_run():
    page = FakePage(status=429, body={"code": 0})
    collector = make_collector(page)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))


def test_ordinary_api_error_is_not_a_risk_stop():
    """普通业务错误应该只失败当前查询，不该中断整轮采集。"""
    page = FakePage(body={"code": 1, "message": "参数错误"})
    collector = make_collector(page)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(collector._same_origin_fetch("/x"))
    assert not isinstance(excinfo.value, RiskControlStop)


class FlakyPage(FakePage):
    """前 `fail_times` 次 fetch 报网络错误，之后正常。

    替身要像真实情况一样**从 evaluate 正常返回**（回一个带 error 的对象），
    而不是抛异常 —— 缺陷正是出在这个区别上：evaluate 成功了，
    所以 `except PlaywrightError` 永远不会触发。
    """

    def __init__(self, fail_times: int, error: str = "TypeError: Failed to fetch", **kw):
        super().__init__(**kw)
        self.fail_times = fail_times
        self.error = error
        self.attempts = 0

    async def evaluate(self, script: str, arg=None) -> dict:
        if arg is None:
            return await super().evaluate(script, arg)
        self.attempts += 1
        if self.attempts <= self.fail_times:
            self.requested_urls.append(arg["url"] if isinstance(arg, dict) else arg)
            return {"status": -1, "text": "", "error": self.error}
        return await super().evaluate(script, arg)


def test_transient_network_error_is_retried_not_abandoned():
    """网络抖一下不该废掉这条岗位。

    2026-08-29 实测：一次网络波动连废 12 条详情。原因是页面内 fetch 抛异常时
    `page.evaluate` 本身是**成功**的（返回 `{status:-1, error:'TypeError: Failed to fetch'}`），
    所以重试循环里那个 `except PlaywrightError` 根本不会触发 —— 一次不重试直接放弃。
    代价是双份的：请求已记账花掉，JD 没拿到，岗位还留在待补队列等下一轮再花一次。
    """
    page = FlakyPage(fail_times=2)
    collector = make_collector(page)
    body = asyncio.run(collector._same_origin_fetch("/wapi/zpgeek/job/detail.json"))
    assert body["code"] == 0, "重试后应当拿到正常响应"
    assert page.attempts == 3, f"应当重试到成功，实际尝试 {page.attempts} 次"


def test_transient_error_gives_up_after_exhausting_attempts():
    """一直失败时要抛错，不能无限重试 —— 每次尝试都在花配额。"""
    page = FlakyPage(fail_times=99)
    collector = make_collector(page)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(collector._same_origin_fetch("/x"))
    assert "同源请求失败" in str(exc.value)
    assert page.attempts == 3, "默认 3 次尝试，别把配额耗在一条注定失败的岗位上"


def test_retry_does_not_mask_risk_control():
    """风控绝不能被当成瞬时错误重试掉。

    风控是**正常响应**（HTTP 200 + 验证页正文或风控 code），不带 error 字段，
    所以判据只看 error 就天然不会误吞它。这条钉死这个边界：
    若有人把判据放宽成"只要失败就重试"，风控就会被重试 3 次，
    等于在已经触发风控的情况下继续加压。
    """
    page = FakePage(body={"code": 37, "message": "您的账户存在异常行为"})
    collector = make_collector(page)
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))


def test_business_errors_are_not_retried():
    """普通业务错误（参数错误之类）重试也不会变好，重试只是白花配额。"""
    page = FakePage(body={"code": 1, "message": "参数错误"})
    collector = make_collector(page)
    with pytest.raises(RuntimeError):
        asyncio.run(collector._same_origin_fetch("/x"))
    assert collector.request_count == 1, "业务错误不该触发重试"


def test_transient_error_signatures_cover_the_common_reports():
    """判据要认全浏览器对连接失败的几种报法，漏一种就等于那种情况下不重试。"""
    from job_radar.collectors.boss import _is_transient_fetch_error as f
    for err in ("TypeError: Failed to fetch", "NetworkError when attempting to fetch",
                "TypeError: Load failed", "net::ERR_NETWORK_CHANGED",
                "net::ERR_NAME_NOT_RESOLVED", "net::ERR_TIMED_OUT"):
        assert f({"error": err}), f"未识别为瞬时错误: {err}"
    # 没有 error 字段的正常响应不是瞬时错误（风控与业务错误都走这条）
    assert not f({"status": 200, "text": "{}"})
    assert not f({"error": ""})


def test_request_budget_stops_run():
    page = FakePage()
    collector = make_collector(page, max_requests_per_run=2)
    asyncio.run(collector._same_origin_fetch("/x"))
    asyncio.run(collector._same_origin_fetch("/x"))
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))
    assert collector.request_count == 2
