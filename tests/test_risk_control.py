from __future__ import annotations

import asyncio
import json

import pytest

from job_radar.collectors.boss import ORIGIN, BossCollector, RiskControlStop
from job_radar.models import CollectionConfig


class FakePage:
    """最小的 Page 替身，只实现 _same_origin_fetch 用到的接口。"""

    def __init__(self, status: int = 200, body: dict | None = None, url: str = ORIGIN):
        self.status = status
        self.body = body if body is not None else {"code": 0, "zpData": {"jobList": []}}
        self.url = url
        self.requested_urls: list[str] = []

    async def evaluate(self, _script: str, arg=None) -> dict:
        # _read_token 传 None，fetch 传 {"url":..., "token":...}。只有 fetch 记录 URL。
        if arg is not None:
            url = arg["url"] if isinstance(arg, dict) else arg
            self.requested_urls.append(url)
        return {"status": self.status, "text": json.dumps(self.body, ensure_ascii=False)}

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
