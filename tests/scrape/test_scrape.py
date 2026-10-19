"""抓取层的单元测试。不碰浏览器、不联网。

只测**判定逻辑**：给什么响应就做什么动作。这几条是踩过的坑，值得钉住：
  · code=37 必须换 token 重试一次，不是直接失败
  · 换过还 37 才是真异常
  · code=36 必须报"要人过验证"，不能当限速退避
  · code=32/35 必须报封禁
  · 「访问频繁」code 仍是 0，要按限速处理
  · 成功响应必须滚 token（否则下一个请求就撞 37）
"""

from __future__ import annotations

import asyncio
import json

import pytest

from boss_scrape.client import BossClient
from boss_scrape.errors import (AccountRestricted, LoginExpired, RiskControl,
                                Throttled, TokenWindowExhausted,
                                VerificationRequired)
from boss_scrape.models import parse_card, parse_detail, parse_salary
from boss_scrape.stoken import RollResult


class FakePage:
    """按脚本依次吐出预设响应。`url` 始终是同源，免得触发重新 bootstrap。"""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.url = "https://www.zhipin.com/web/geek/jobs"
        self.calls = 0

    async def evaluate(self, _script, args=None):
        self.calls += 1
        if not self.responses:
            raise AssertionError("没有更多预设响应")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def content(self) -> str:
        return "<html></html>"


class FakeRoller:
    def __init__(self, works: bool = True) -> None:
        self.calls = 0
        self.works = works

    async def roll(self) -> RollResult:
        self.calls += 1
        return (RollResult(True, "rolled", sname="f45bbe80", token_length=469)
                if self.works else RollResult(False, "no-seed"))


def make_client(responses: list[dict], *, roller_works: bool = True) -> BossClient:
    client = BossClient(profile=".", delay=(0.0, 0.0), burst_pause=(0.0, 0.0),
                        burst_every=1000)
    client.page = FakePage(responses)
    client.roller = FakeRoller(works=roller_works)
    return client


def ok(payload: dict | None = None) -> dict:
    body = {"code": 0, "message": "Success", "zpData": payload or {}}
    return {"status": 200, "text": json.dumps(body)}


def code(value: int, message: str = "") -> dict:
    return {"status": 200, "text": json.dumps({"code": value, "message": message})}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 风控分类


def test_success_rolls_the_token_so_the_next_request_has_a_window():
    """成功响应必须滚 token —— 不滚的话第 6 个请求就撞 37。"""
    client = make_client([ok({"jobList": []})])
    body = run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert body["code"] == 0
    assert client.roller.calls == 1
    assert client.stats.rolls == 1


def test_code_37_rolls_then_retries_once():
    client = make_client([code(37, "您的环境存在异常."), ok({"jobList": []})])
    body = run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert body["code"] == 0
    assert client.stats.retried_37 == 1
    assert client.page.calls == 2


def test_code_37_twice_is_a_real_failure_not_a_loop():
    client = make_client([code(37, "您的环境存在异常."), code(37, "您的环境存在异常.")])
    with pytest.raises(TokenWindowExhausted):
        run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert client.page.calls == 2


def test_code_37_without_a_usable_roller_fails_fast():
    """滚动不可用时不能假装重试——那只会多打一次平台。"""
    client = make_client([code(37, "您的环境存在异常.")])
    client.roller = FakeRoller(works=False)
    with pytest.raises(TokenWindowExhausted):
        run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert client.page.calls == 1


def test_code_36_asks_for_human_verification():
    client = make_client([code(36, "您的账户存在异常行为.")])
    with pytest.raises(VerificationRequired) as excinfo:
        run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert "人工过验证" in str(excinfo.value)


def test_ban_codes_are_account_restricted():
    for value in (32, 35):
        client = make_client([code(value, "您的账户存在异常行为，已暂时被禁止使用")])
        with pytest.raises(AccountRestricted) as excinfo:
            run(client._get("/wapi/zpgeek/search/joblist.json"))
        assert excinfo.value.code == value


def test_login_expiry_is_its_own_signal():
    client = make_client([code(7, "登录信息失效")])
    with pytest.raises(LoginExpired):
        run(client._get("/wapi/zpgeek/search/joblist.json"))


def test_throttle_message_with_code_zero_is_rate_limiting():
    """「访问频繁」的 code 是 0 —— 按 code 判会把它当成成功。"""
    client = make_client([{"status": 200,
                           "text": json.dumps({"code": 0, "message": "访问频繁，请稍后再试"})},
                          ok({"jobList": []})])
    body = run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert body["code"] == 0
    assert client.stats.throttled == 1


def test_verification_html_is_not_treated_as_a_format_error():
    client = make_client([{"status": 200, "text": "<html>请完成安全验证</html>"}])
    with pytest.raises(VerificationRequired):
        run(client._get("/wapi/zpgeek/search/joblist.json"))


def test_http_429_is_risk_control():
    client = make_client([{"status": 429, "text": ""}])
    with pytest.raises(RiskControl):
        run(client._get("/wapi/zpgeek/search/joblist.json"))


def test_unknown_code_is_reported_with_its_value():
    client = make_client([code(9999, "未知")])
    with pytest.raises(RiskControl) as excinfo:
        run(client._get("/wapi/zpgeek/search/joblist.json"))
    assert excinfo.value.code == 9999


# ---------------------------------------------------------------- 字段解析


def test_salary_variants():
    assert parse_salary("20-40K·14薪") == (20.0, 40.0, 14)
    assert parse_salary("1.5-2.5万") == (15.0, 25.0, None)
    assert parse_salary("15-25K") == (15.0, 25.0, None)
    low, high, months = parse_salary("200-300元/天")
    assert months is None and low is not None and high is not None


def test_card_requires_a_job_id():
    with pytest.raises(ValueError):
        parse_card({"jobName": "无 ID"})


def test_card_maps_platform_fields():
    card = parse_card({
        "encryptJobId": "abc", "jobName": "AI 工程师", "brandName": "某公司",
        "cityName": "杭州", "areaDistrict": "西湖区", "businessDistrict": "文三路",
        "salaryDesc": "30-50K·15薪", "jobExperience": "3-5年", "jobDegree": "本科",
        "skills": ["Python", "RAG"], "jobLabels": ["大模型"], "welfareList": ["五险一金"],
        "jobType": 0, "securityId": "sec-1", "lid": "lid-1",
    })
    assert card.job_id == "abc"
    assert card.url == "https://www.zhipin.com/job_detail/abc.html"
    assert card.location_text == "杭州·西湖区·文三路"
    assert (card.salary_min_k, card.salary_max_k, card.salary_months) == (30.0, 50.0, 15)
    assert card.skills == ["Python", "RAG"]


def test_detail_reads_description_from_job_info_not_the_root():
    """正文在 `zpData.jobInfo.postDescription`。读根节点会得到空字符串。"""
    detail = parse_detail({
        "jobInfo": {"encryptId": "abc", "postDescription": "岗位职责……",
                    "encryptUserId": "u1", "showSkills": ["Go"]},
        "bossInfo": {"activeTimeDesc": "3日内活跃", "certificated": True},
        "brandComInfo": {"introduce": "成立于2015年", "scaleName": "100-499人"},
        "securityId": "next-sec", "lid": "next-lid",
    }, job_id="abc")
    assert detail.description == "岗位职责……"
    assert detail.recruiter_active_days == 3
    assert detail.company_founded_year == 2015
    assert detail.next_security_id == "next-sec"
    assert detail.skills == ["Go"]


def test_detail_rejects_a_mismatched_job_id():
    with pytest.raises(RuntimeError):
        parse_detail({"jobInfo": {"encryptId": "other", "postDescription": "x"}},
                     job_id="abc")


def test_detail_rejects_an_empty_description():
    with pytest.raises(RuntimeError):
        parse_detail({"jobInfo": {"encryptId": "abc", "postDescription": "   "}},
                     job_id="abc")


# ---------------------------------------------------------------- 运行日志


def test_runlog_writes_jsonl_and_summarize_counts_risk_rate(tmp_path):
    """日志要能回答"风控前 60 秒发了多少请求"——阈值只能从速率反推。"""
    from boss_scrape.runlog import RunLog, summarize

    path = tmp_path / "scrape.jsonl"
    log = RunLog(path)
    log.write("start", delay=[0, 0])
    for index in range(1, 7):
        log.write("request", path="/wapi/x", http=200, code=0,
                  requests=index, rolls=index - 1, ms=50)
    log.write("roll", ok=True, reason="rolled", sname="f45bbe80", token_len=469)
    log.write("request", path="/wapi/x", http=200, code=36, message="账户异常",
              requests=7, rolls=6, ms=50)
    log.write("risk", path="/wapi/x", code=36, handled="human_verification")
    log.write("end", requests=7, rolls=6)

    summary = summarize(path, window_s=60)
    assert summary["requests"] == 7
    assert summary["codes"] == {"0": 6, "36": 1}
    assert summary["rolls"] == 1
    assert summary["risks"] == 1
    assert summary["risk_codes"] == {"36": 1}
    assert summary["rate_at_risk"][0]["requests_in_window"] == 7


def test_runlog_never_records_credentials(tmp_path):
    """日志是样本，不是凭据库：token / 种子值不能进去。"""
    from boss_scrape.runlog import RunLog

    path = tmp_path / "scrape.jsonl"
    log = RunLog(path)
    log.write("roll", ok=True, reason="rolled", sname="f45bbe80", token_len=469)
    text = path.read_text(encoding="utf-8")
    assert "token_len" in text
    assert "0a14g" not in text  # token 的前缀形状不该出现


def test_runlog_without_path_is_a_no_op():
    from boss_scrape.runlog import RunLog

    log = RunLog(None)
    log.write("request", code=0)
    assert log.enabled is False
    assert log.entries == 0
