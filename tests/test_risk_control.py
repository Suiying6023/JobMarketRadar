from __future__ import annotations

import asyncio
import json

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig


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


def test_request_budget_stops_run():
    page = FakePage()
    collector = make_collector(page, max_requests_per_run=2)
    asyncio.run(collector._same_origin_fetch("/x"))
    asyncio.run(collector._same_origin_fetch("/x"))
    with pytest.raises(RiskControlStop):
        asyncio.run(collector._same_origin_fetch("/x"))
    assert collector.request_count == 2
