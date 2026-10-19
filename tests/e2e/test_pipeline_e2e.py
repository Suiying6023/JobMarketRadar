"""采集线路的端到端测试。

每个用例都走完整链路：构造请求 → 真的发 HTTP → 读状态码 → 解码正文 →
判定风控 → 记账 → 解析入库。断言的是**可观测的外部行为**（发了几个请求、
落了几条账、库里有几条、策略文件变成什么），不是内部函数的返回值。

替换掉的旧测试里，同一批不变量被拆成了 37 个用例（test_strict_budget）、
30 个（test_safety_gate）、18 个（test_request_ledger）、15 个（test_risk_control），
大多靠手写 FakePage 返回 dict 来验证分支。合并到这里之后，分支覆盖靠真实响应驱动。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from job_radar.collectors import boss as boss_module
from job_radar.collectors.boss import (
    API_PATH,
    DETAIL_PATH,
    ORIGIN,
    USER_INFO_PATH,
    BossCollector,
    PlatformRiskStop,
)
from job_radar.collectors.stoken import RollResult
from job_radar.models import CityConfig, CollectionConfig, SearchFilters
from job_radar.safety import AccessBlocked, AccessGate
from job_radar.storage import JobStore
from tests.support.replay import ReplayPage, ReplayServer

FIXTURES = Path(__file__).parent.parent / "fixtures"
LIST_PAGE1 = json.loads((FIXTURES / "boss_list_multi.json").read_text(encoding="utf-8"))
DETAIL = json.loads((FIXTURES / "boss_job_detail.json").read_text(encoding="utf-8"))
HANGZHOU = CityConfig(name="杭州", boss_code="101210100")


def page2_payload() -> dict:
    """第二页给两条不同的岗位，并声明没有更多。"""
    return {
        "code": 0,
        "message": "Success",
        "zpData": {
            "lid": "e2eLidPage2",
            "hasMore": False,
            "jobList": [
                {
                    "encryptJobId": "e2eJobDelta004",
                    "securityId": "e2eSecurityDelta",
                    "jobName": "智能体应用工程师",
                    "brandName": "示例机器人",
                    "brandIndustry": "智能硬件",
                    "brandScaleName": "500-999人",
                    "brandStageName": "B轮",
                    "encryptBrandId": "e2eBrandDelta",
                    "cityName": "杭州",
                    "areaDistrict": "滨江区",
                    "businessDistrict": "江南大道",
                    "salaryDesc": "18-30K",
                    "jobExperience": "经验不限",
                    "jobDegree": "本科",
                    "skills": ["Python", "Agent"],
                    "welfareList": ["五险一金"],
                    "bossName": "赵女士",
                    "bossTitle": "HR",
                    "bossOnline": True,
                },
                {
                    "encryptJobId": "e2eJobEpsilon005",
                    "securityId": "e2eSecurityEpsilon",
                    "jobName": "数据分析工程师",
                    "brandName": "示例分析",
                    "brandIndustry": "大数据",
                    "brandScaleName": "100-499人",
                    "brandStageName": "C轮",
                    "encryptBrandId": "e2eBrandEpsilon",
                    "cityName": "杭州",
                    "areaDistrict": "拱墅区",
                    "businessDistrict": "运河",
                    "salaryDesc": "12-20K",
                    "jobExperience": "1-3年",
                    "jobDegree": "本科",
                    "skills": ["SQL", "Python"],
                    "welfareList": ["五险一金"],
                    "bossName": "孙先生",
                    "bossTitle": "数据负责人",
                    "bossOnline": False,
                },
            ],
        },
    }


def list_router(query: dict) -> dict:
    page = (query.get("page") or ["1"])[0]
    return LIST_PAGE1 if page == "1" else page2_payload()


# 详情接口按 securityId 取岗位，响应里的 encryptId 必须与请求的岗位一致，
# 否则 `apply_detail` 会以「详情响应岗位 ID 与请求岗位不一致」拒收——这条校验
# 是防止把 A 岗位的正文写进 B 岗位。
SECURITY_TO_JOB = {
    "e2eSecurityAlpha": "e2eJobAlpha001",
    "e2eSecurityBeta": "e2eJobBeta002",
    "e2eSecurityGamma": "e2eJobGamma003",
    "e2eSecurityDelta": "e2eJobDelta004",
    "e2eSecurityEpsilon": "e2eJobEpsilon005",
}


def detail_router(query: dict) -> dict:
    security_id = (query.get("securityId") or [""])[0]
    job_id = SECURITY_TO_JOB.get(security_id, "")
    payload = json.loads(json.dumps(DETAIL))  # 深拷贝，避免用例之间互相污染
    payload["zpData"]["jobInfo"]["encryptId"] = job_id
    payload["zpData"]["securityId"] = f"rotated-{security_id}"
    payload["zpData"]["lid"] = "e2eLidDetail"
    return payload


@pytest.fixture(autouse=True)
def no_wall_clock_waits(monkeypatch):
    """把采集器里的随机等待压成 0。

    这里只去掉**挂钟时间**，不动任何判定逻辑：`_goto_search` 导航后固定睡 2.5-4 秒，
    重试退避也是随机秒数。这些是节奏参数，不是被测行为；让它们真睡只会把套件从
    几秒拖到几十秒，还会让「请求发了几个」这类断言依赖超时。
    """
    monkeypatch.setattr(boss_module.random, "uniform", lambda a, b: 0.0)


@pytest.fixture
def server():
    s = ReplayServer()
    s.route(API_PATH, list_router)
    s.route(DETAIL_PATH, detail_router)
    s.route(USER_INFO_PATH, {"code": 0, "zpData": {"userId": 10001}})
    s.route("/web/geek/jobs", "<html><body>search page</body></html>")
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def ledger(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite3") as store:
        yield store


@pytest.fixture
def gate(tmp_path):
    g = AccessGate(tmp_path / "access-policy.json")
    g.release("端到端测试显式放行")
    return g


def make_collector(server: ReplayServer, *, gate, ledger, **overrides) -> BossCollector:
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        "network_enabled": True,
        # 真实节奏在测试里没有意义，只留出 0 延迟；节流逻辑由 config 值驱动，测它没有收益。
        "request_delay_seconds": [0.0, 0.0],
        "detail_delay_seconds": [0.0, 0.0],
        "detail_batch": 50,
        "detail_cooldown_seconds": [0.0, 0.0],
        "burst_threshold": 100,
        "burst_penalty_seconds": [0.0, 0.0],
        **overrides,
    }
    collector = BossCollector(CollectionConfig.model_validate(payload))
    collector.page = ReplayPage(server)  # type: ignore[assignment]
    collector.gate = gate
    collector.ledger = ledger
    collector.ledger_script = "e2e"
    return collector


def drain(collector: BossCollector, *, pages: int = 2, detail_limit: int = 0):
    async def run():
        out = []
        async for job in collector.collect(HANGZHOU, "AI Agent", SearchFilters(), pages, detail_limit):
            out.append(job)
        return out

    return asyncio.run(run())


# ---------------------------------------------------------------- 正常路径


def test_collect_stores_every_card_and_ledger_matches_http_calls(server, gate, ledger):
    """翻两页：5 条岗位全部入库，账本行数等于实际发出的 HTTP 请求数。

    「账本行数 == 真实请求数」是这个系统最重要的一条不变量：账本是判断
    「今天发了多少」的唯一依据，记漏了就会在不知不觉中超额。
    """
    collector = make_collector(server, gate=gate, ledger=ledger)
    jobs = drain(collector, pages=2)

    assert [j.source_job_id for j in jobs] == [
        "e2eJobAlpha001", "e2eJobBeta002", "e2eJobGamma003",
        "e2eJobDelta004", "e2eJobEpsilon005",
    ]
    # 导航一次 + 两页列表，一共三次真实请求
    assert len(server.calls_to(API_PATH)) == 2
    assert len(server.calls) == 3
    rows = ledger.conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
    assert rows == 3, f"账本记了 {rows} 行，实际发了 {len(server.calls)} 次请求"


def test_request_script_carries_the_headers_the_platform_expects(server, gate, ledger):
    """请求头写在交给浏览器的 JS 里，Python 侧看不见——所以直接断言那段脚本。

    这条测试守的是一个真实踩过的坑：headers 必须嵌在 `fetch(url, {headers})` 的
    init 里；写成 `fetch(url, headers)` 时 fetch 会静默丢弃全部自定义头，
    而 `credentials` 恰好是合法键，于是 cookie 照带、请求照样成功，bug 被完全掩盖。
    """
    collector = make_collector(server, gate=gate, ledger=ledger)
    drain(collector, pages=1)
    script = collector.page.scripts[0]

    assert "X-Requested-With': 'XMLHttpRequest'" in script
    assert "headers['zp_token'] = bst" in script
    assert "credentials: 'include'" in script
    # 关键：headers 必须作为 init 对象的字段，不能当成 fetch 的第二个参数
    assert "fetch(url, {credentials" in script.replace("'", "'")
    assert "fetch(url, headers)" not in script


def test_jobs_are_kept_unless_a_title_filter_says_otherwise(server, gate, ledger):
    """默认没有标题过滤时，销售岗也会以 active 入库——过滤是配置出来的，不是内置的。

    这条把「配置里没写就等于不过滤」这件事钉住：默认配置的
    `exclude_title_keywords: []` 意味着采集器不会替你判断岗位方向。
    """
    collector = make_collector(server, gate=gate, ledger=ledger)
    jobs = drain(collector, pages=1)
    by_id = {j.source_job_id: j for j in jobs}
    assert by_id["e2eJobGamma003"].retention_status == "active"


def test_title_exclusion_downgrades_matching_jobs_to_sample(server, gate, ledger):
    """配了标题排除词之后，命中的岗位降级为 sample（仍入库，但不进复核队列）。"""
    collector = make_collector(
        server, gate=gate, ledger=ledger, exclude_title_keywords=["销售"]
    )
    jobs = drain(collector, pages=1)
    by_id = {j.source_job_id: j for j in jobs}
    assert by_id["e2eJobGamma003"].retention_status == "sample"
    assert by_id["e2eJobAlpha001"].retention_status == "active"


# ---------------------------------------------------------------- 详情路径


def test_detail_enrichment_fills_body_and_records_login_evidence(server, gate, ledger):
    """详情链路要过登录闸门：先问 getUserInfo，确认服务端认这个账号，才允许落正文。"""
    collector = make_collector(server, gate=gate, ledger=ledger, collect_details=True)
    jobs = drain(collector, pages=1, detail_limit=1)

    detailed = [j for j in jobs if j.job_description]
    assert len(detailed) == 1
    job = detailed[0]
    assert "Agent核心开发" in job.job_description
    assert job.recruiter_active_status == "刚刚活跃"
    # 登录态证据必须留下，否则事后无法区分「核验全文」和「匿名预览」
    assert job.raw["detail_source"]["access_evidence"]["authenticated"] is True
    assert len(server.calls_to(USER_INFO_PATH)) == 1


def test_detail_rejects_body_whose_job_id_does_not_match_the_request(server, gate, ledger):
    """详情响应里的岗位 ID 与请求的不一致时，**不能写库**。

    这条守的是「串号」：平台在并发/缓存异常时可能把 A 岗位的正文回给 B 的请求。
    没有这道校验，B 的 JD 就会被 A 的内容覆盖，而且事后完全看不出来——
    数据是自洽的、格式是对的，只是内容属于另一个岗位。
    """
    def wrong_detail(query: dict) -> dict:
        payload = detail_router(query)
        payload["zpData"]["jobInfo"]["encryptId"] = "someOtherJobId"
        return payload

    server.route(DETAIL_PATH, wrong_detail)
    collector = make_collector(server, gate=gate, ledger=ledger, collect_details=True)
    jobs = drain(collector, pages=1, detail_limit=1)

    assert jobs, "列表阶段仍然要产出岗位"
    # 一条正文都不许落，而且失败原因要留在岗位上，不能静默
    assert all(not j.job_description for j in jobs)
    assert "岗位 ID" in jobs[0].raw.get("detail_error", "")


def test_detail_refuses_when_server_does_not_confirm_login(server, gate, ledger):
    """服务端不认这个账号时必须停下，且**不能**把访问策略冻结。

    「未登录」是本机会话问题，不是平台风控。把它写进持久策略会让下次启动直接被拦住，
    而真正需要人工处理的其实是「去浏览器里重新登录」——两件事的处置完全不同，
    所以这里只停本进程、不落冻结，并给出可判别的 category。
    """
    server.route(USER_INFO_PATH, {"code": 0, "zpData": {"userId": 0}})
    collector = make_collector(server, gate=gate, ledger=ledger, collect_details=True)
    with pytest.raises(PlatformRiskStop) as excinfo:
        drain(collector, pages=1, detail_limit=1)
    assert excinfo.value.category == "authentication_required"
    assert "登录" in str(excinfo.value)
    # 关键：不冻结持久策略
    assert gate.read_state().mode == "ready"
    # 而且一条详情请求都没发出去
    assert server.calls_to(DETAIL_PATH) == []


# ---------------------------------------------------------------- 风控与熔断


def test_risk_code_pauses_policy_and_sends_no_further_request(server, gate, ledger):
    """命中 code=32：当场持久熔断 + 停止，且**之后不再发任何请求**。"""
    server.route(API_PATH, {"code": 32, "message": "您的账户存在异常行为，已暂时被禁止使用"})
    collector = make_collector(server, gate=gate, ledger=ledger)

    with pytest.raises(PlatformRiskStop) as excinfo:
        drain(collector, pages=3)

    assert excinfo.value.code == 32
    state = gate.read_state()
    assert state.mode == "paused"
    assert state.manual_release_required is True
    # 熔断发生在第一次列表请求之后，第 2、3 页不能再发出去
    assert len(server.calls_to(API_PATH)) == 1
    outcomes = [r[0] for r in ledger.conn.execute("SELECT outcome FROM request_log")]
    assert outcomes == ["navigation", "risk"]


def test_verification_html_page_trips_risk_instead_of_being_parsed(server, gate, ledger):
    """HTTP 200 但正文是验证页 HTML —— 必须按风控停机，不能当成「格式错误的普通响应」。"""
    server.route(API_PATH, "<html><body>请完成安全验证</body></html>")
    collector = make_collector(server, gate=gate, ledger=ledger)
    with pytest.raises(PlatformRiskStop):
        drain(collector, pages=1)
    assert gate.read_state().mode == "paused"
    assert len(server.calls_to(API_PATH)) == 1


def test_throttle_message_is_not_treated_as_account_ban(server, gate, ledger):
    """HTTP 200 + code=0 + 「访问频繁」是限速，不是封号：账本记 throttled，严重度要分开。"""
    server.route(API_PATH, {"code": 0, "message": "访问频繁，请稍后再试"})
    collector = make_collector(server, gate=gate, ledger=ledger)
    with pytest.raises(PlatformRiskStop):
        drain(collector, pages=1)
    outcomes = [r[0] for r in ledger.conn.execute("SELECT outcome FROM request_log")]
    assert outcomes == ["navigation", "throttled"]


# ---------------------------------------------------------------- 刷新链路


def test_refresh_verdict_never_downgrades_on_uncertainty():
    """刷新链路的判定矩阵：**不确定一律返回 None，绝不猜「已关闭」**。

    方向是刻意的：误判关闭会单向降级一个在招岗位（不可逆，且没有回头路），
    误判在招只是下一轮再刷一次（可逆且便宜）。所以这里把「什么情况下必须弃权」
    全部钉死——挑战页、状态码异常、URL 对不上、标题对不上、平台新文案。
    """
    from job_radar.service_jobs import status_verdict

    row = {"job_url": "https://www.zhipin.com/job_detail/abc.html", "title": "AI Agent 开发工程师"}
    good_url = "https://www.zhipin.com/job_detail/abc.html"

    # 需要人工过验证：必须抛，不能返回 None 蒙混过去
    for evidence in (
        {"status": 403, "url": good_url},
        {"status": 200, "challenged": True, "url": good_url},
    ):
        with pytest.raises(PlatformRiskStop):
            status_verdict(row, evidence)

    # 明确关闭：只有 404 + 页面不存在才算
    assert status_verdict(row, {"status": 404, "gone": True, "url": good_url}) is True
    # 明确在招
    assert status_verdict(row, {"status": 200, "url": good_url, "h1": "AI Agent 开发工程师",
                                "job_status": "招聘中"}) is False
    assert status_verdict(row, {"status": 200, "url": good_url, "h1": "AI Agent 开发工程师",
                                "job_status": "职位已关闭"}) is True

    # 以下全部必须弃权（None）
    assert status_verdict(row, {"status": 200, "url": good_url, "h1": "AI Agent 开发工程师",
                                "job_status": "平台新增的某种文案"}) is None
    assert status_verdict(row, {"status": 200, "url": good_url, "h1": "销售代表",
                                "job_status": "职位已关闭"}) is None, "标题对不上就不能采信关闭"
    assert status_verdict(row, {"status": 200, "url": "https://www.zhipin.com/job_detail/zzz.html",
                                "h1": "AI Agent 开发工程师", "job_status": "职位已关闭"}) is None, \
        "URL 对不上说明被重定向了，不能张冠李戴"
    assert status_verdict(row, {"status": 500, "url": good_url,
                                "h1": "AI Agent 开发工程师"}) is None
    assert status_verdict(row, {"status": 200, "url": good_url,
                                "h1": "AI Agent 开发工程师"}) is None, "缺 job_status 容器不能猜"


def test_refresh_scope_gate_is_independent_from_collect_gate(tmp_path):
    """刷新的暂停和采集的暂停是**两个文件、两个 scope**，互不牵连。

    这条守的是一个真实风险：如果两者共用一个策略文件，那么「刷新暂停」会连带
    停掉采集，或者更糟——「采集已放行」会被误读成「刷新也放行」，于是匿名跳板
    链路在没人同意的情况下跑起来。
    """
    from job_radar.safety import AccessGate

    db = tmp_path / "jobs.sqlite3"
    db.touch()
    collect_gate = AccessGate(db)
    refresh_gate = AccessGate(db, scope="refresh")

    assert collect_gate.state_path.name != refresh_gate.state_path.name

    collect_gate.release("采集放行")
    refresh_gate.pause("刷新暂停")

    # 采集能过，刷新被拦 —— 两者互不影响
    collect_gate.require_online(True)
    with pytest.raises(AccessBlocked):
        refresh_gate.require_online(True)

    # 反过来也一样
    refresh_gate.release("刷新放行")
    collect_gate.pause("采集暂停")
    refresh_gate.require_online(True)
    with pytest.raises(AccessBlocked):
        collect_gate.require_online(True)


def test_request_log_never_blocks_a_request_and_keeps_recording(server, gate, ledger):
    """账本只记录、不放行：当天已有记录也不会拦下任何一次请求。

    旧机制把 `max_requests_per_day` 当硬线，日线一到就在发请求之前抛
    `BudgetExceeded`。那条日线是按历史轮次反推的推测值，不是平台给的额度，
    所以现在只保留观测：预置一行当天记录后，采集照常跑完，每次尝试各落一行。
    """
    ledger.conn.execute(
        "INSERT INTO request_log(ts, day, kind, outcome, code, script, note) "
        "VALUES (datetime('now'), date('now'), 'list', 'ok', NULL, 'e2e', 'pre-filled')"
    )
    ledger.conn.commit()

    collector = make_collector(server, gate=gate, ledger=ledger)
    jobs = drain(collector, pages=1)

    assert [j.source_job_id for j in jobs] == [
        "e2eJobAlpha001", "e2eJobBeta002", "e2eJobGamma003",
    ]
    # 预置那一行没有拦住任何请求：导航 + 一页列表都真实发了出去
    assert len(server.calls_to(API_PATH)) == 1
    assert len(server.calls) == 2
    rows = ledger.conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
    assert rows == 3, "预置 1 行 + 本轮 2 次真实请求各记 1 行"


def test_expired_login_stops_without_freezing_the_policy(server, gate, ledger):
    """HTTP 200 + code=7「当前登录状态已失效」：停止，但**不冻结**访问策略。

    这是 2026-10-18 真实线路跑出来的形态：上次登录是 9-30，18 天后 getUserInfo
    直接回 code=7，导航本身还是 200。它不是封号，正确处置是去浏览器重新登录；
    如果把它当风控冻结策略，「登录完继续跑」反而会被自己拦住。
    同时它也不该退化成一句 `RuntimeError: BOSS 接口错误 code=7`——日志里看不出该干什么。
    """
    server.route(API_PATH, {"code": 7, "message": "当前登录状态已失效"})
    collector = make_collector(server, gate=gate, ledger=ledger)

    with pytest.raises(PlatformRiskStop) as excinfo:
        drain(collector, pages=1)

    assert excinfo.value.category == "authentication_required"
    assert "重新登录" in str(excinfo.value)
    assert gate.read_state().mode == "ready", "登录过期不该冻结访问策略"
    outcomes = [r[0] for r in ledger.conn.execute("SELECT outcome FROM request_log")]
    assert outcomes[-1] == "authentication"


def test_a_full_days_log_does_not_stop_the_next_run(server, gate, ledger):
    """当天账本已经很满（旧默认日线 400 行）时，下一轮照样发请求、照样记账。

    旧实现在这里会在发请求之前停下；现在没有任何日线参与放行决定，
    唯一会停的是访问策略闸门和平台风控响应。
    """
    ledger.conn.executemany(
        "INSERT INTO request_log(ts, day, kind, outcome, code, script, note) "
        "VALUES (datetime('now'), date('now'), 'list', 'ok', NULL, 'e2e', 'pre-filled')",
        [()] * 400,
    )
    ledger.conn.commit()

    collector = make_collector(server, gate=gate, ledger=ledger)
    jobs = drain(collector, pages=2)

    assert len(jobs) == 5
    assert len(server.calls_to(API_PATH)) == 2
    rows = ledger.conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
    assert rows == 403, "400 行预置 + 本轮 3 次真实请求各记 1 行"


def test_second_query_reuses_the_loaded_page_instead_of_navigating_again(server, gate, ledger):
    """窗口没用完、页面也没漂走时，不重复导航。

    导航的真实作用是**重铸 `__zp_stoken__`**（实测一个窗口约 5 个请求），
    所以判断依据必须是**请求数**，不是查询数。这里 2 个查询共 2 个列表请求，
    远没到窗口上限，应当只导航一次。

    这条同时钉住反面：**页面离开 BOSS 源时必须重新导航**，不能盲目复用。
    """
    collector = make_collector(server, gate=gate, ledger=ledger)

    def one_query(keyword: str):
        async def run():
            return [j async for j in collector.collect(HANGZHOU, keyword, SearchFilters(), 1, 0)]

        return asyncio.run(run())

    assert len(one_query("AI Agent")) == 3
    assert len(one_query("Python")) == 3
    assert len(collector.page.navigations) == 1, collector.page.navigations
    assert len(server.calls_to("/web/geek/jobs")) == 1

    # 页面漂到非 BOSS 源（实测里最常见的是漂到 about:blank）时必须重新导航。
    # 注意 /web/user/ 这类同源登录页**不算漂走**：只要还在 www.zhipin.com 上，
    # 页面内 fetch 依然带得上 cookie，不需要重导航。
    collector.page.url = "about:blank"
    assert len(one_query("RAG")) == 3
    assert len(collector.page.navigations) == 2, collector.page.navigations


def test_token_window_forces_a_recast_before_the_platform_rejects(server, gate, ledger):
    """窗口按**请求数**算：到了上限必须重铸，否则下一个请求就被平台打回 code=37。

    实测依据（2026-10-18，已登录会话，间隔 6 秒）：
      第 5 个请求成功 → 第 6 个 `code=37`「您的环境存在异常.」
      干等 30 秒仍是 37；干等 90 秒仍是 37；做一次页面加载才恢复。

    所以「每个查询导航一次」不是浪费，是续窗口。这条把阈值钉住：
    `remint_after_requests=2` 时，第三个查询必须触发重铸。
    """
    collector = make_collector(server, gate=gate, ledger=ledger, remint_after_requests=2)

    def one_query(keyword: str):
        async def run():
            return [j async for j in collector.collect(HANGZHOU, keyword, SearchFilters(), 1, 0)]

        return asyncio.run(run())

    one_query("AI Agent")   # 导航 1（清零）→ 列表 1（计数 1）
    one_query("Python")     # 计数 1 < 2，不导航 → 列表 2（计数 2）
    assert len(collector.page.navigations) == 1

    one_query("RAG")        # 计数 2 >= 2 → 必须重铸
    assert len(collector.page.navigations) == 2, collector.page.navigations

    one_query("FastAPI")    # 重铸后清零，不该再导航
    assert len(collector.page.navigations) == 2, collector.page.navigations


def test_paused_policy_blocks_collection_before_the_browser_is_used(server, gate, ledger):
    """策略是 paused 时，连浏览器都不该被用到。"""
    gate.pause("端到端测试：模拟人工巡检发现风控")
    collector = make_collector(server, gate=gate, ledger=ledger)
    with pytest.raises(AccessBlocked):
        drain(collector, pages=1)
    assert server.calls == []


def test_offline_switch_blocks_collection(server, gate, ledger):
    """配置里的在线总开关关掉时，同样一个请求都不发。"""
    collector = make_collector(server, gate=gate, ledger=ledger, network_enabled=False)
    with pytest.raises(AccessBlocked):
        drain(collector, pages=1)
    assert server.calls == []


# ---------------------------------------------------------------- stoken 滚动
#
# 实测依据（2026-10-18，已登录会话）：同一个 `__zp_stoken__` 只允许 5 个请求，
# 第 6 个回 `code=37`；但每个 `code=0` 响应都会下发新种子
# （`__zp_sseed__` / `__zp_sname__` / `__zp_sts__`），就地能算出新 token，
# 成本 0 个请求。连续 20 个请求 20/20 全过。
#
# 这里用替身，因为真滚动要跑平台 319KB 的签名脚本；测的是**采集器的编排**：
# 什么时候滚、滚完要不要导航、滚失败怎么退回。


class _FakeRoller:
    """记录调用次数的最小替身。`works=False` 模拟种子缺失/签名脚本拉不到。"""

    def __init__(self, *, works: bool = True) -> None:
        self.calls = 0
        self.works = works

    async def roll(self) -> RollResult:
        self.calls += 1
        if not self.works:
            return RollResult(False, "no-seed", error="缺种子: __zp_sseed__")
        return RollResult(True, "rolled", sname="f45bbe80", token_length=469)


def test_stoken_roll_renews_the_window_without_navigating(server, gate, ledger):
    """开着滚动时，窗口由就地重算续上，导航只发生在第一次。

    把 `remint_after_requests` 压到 1：如果没有滚动，每发一个请求就越线，
    每次查询都得导航一次。滚动生效后计数每次清零，所以始终只有起手那一次导航。
    """
    collector = make_collector(server, gate=gate, ledger=ledger, remint_after_requests=1)
    roller = _FakeRoller()
    collector._stoken_roller = roller

    def one_query(keyword: str):
        async def run():
            return [j async for j in collector.collect(HANGZHOU, keyword, SearchFilters(), 1, 0)]

        return asyncio.run(run())

    for keyword in ("AI Agent", "Python", "RAG", "FastAPI"):
        assert len(one_query(keyword)) == 3

    assert len(collector.page.navigations) == 1, collector.page.navigations
    assert len(server.calls_to("/web/geek/jobs")) == 1
    # 4 个查询各 1 个列表请求 → 4 次滚动，且没有额外请求
    assert roller.calls == 4, roller.calls
    assert collector._requests_since_remint == 0


def test_stoken_roll_failure_falls_back_to_navigation(server, gate, ledger):
    """滚动失败必须退回按请求数导航——否则窗口会悄悄耗尽，下一个请求就撞 37。"""
    collector = make_collector(server, gate=gate, ledger=ledger, remint_after_requests=2)
    roller = _FakeRoller(works=False)
    collector._stoken_roller = roller

    def one_query(keyword: str):
        async def run():
            return [j async for j in collector.collect(HANGZHOU, keyword, SearchFilters(), 1, 0)]

        return asyncio.run(run())

    one_query("AI Agent")   # 导航 1 → 列表 1（计数 1，滚动失败不清零）
    one_query("Python")     # 计数 2 → 列表 2
    assert len(collector.page.navigations) == 1

    one_query("RAG")        # 计数 2 >= 2 → 必须导航重铸
    assert len(collector.page.navigations) == 2, collector.page.navigations
    assert roller.calls >= 3, roller.calls


def test_risk_response_is_not_rolled(server, gate, ledger):
    """风控响应不下发种子，滚动没有意义；而且此时不该再对平台做任何动作。"""
    server.route(API_PATH, {"code": 37, "message": "您的环境存在异常."})
    collector = make_collector(server, gate=gate, ledger=ledger)
    roller = _FakeRoller()
    collector._stoken_roller = roller

    with pytest.raises(PlatformRiskStop) as excinfo:
        drain(collector, pages=1)

    assert excinfo.value.code == 37
    assert roller.calls == 0, "风控响应后不应再触发滚动"
    assert gate.read_state().mode == "paused"
