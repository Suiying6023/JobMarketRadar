"""补详情的选取口径测试。

覆盖 2026-08-29 发现的一次真实浪费：`backfill_details.py` 只按
`job_description = ''` 选行，不看这条记录是否注定被 `clean_local_db` 降级。
实测 5054 条待补详情里 2259 条注定降级（薪资超纲 1332、匿名公司 778、
非目标城市 129……）——一条详情一次请求，全跑完等于白花 125% 的日线额度。

采集侧的 `_should_keep` 拦不到这些：它只在**列表阶段**跑，而补详情是直接
读库的存量记录，绕过了那一层。所以这里必须自己筛。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_details as B  # noqa: E402
from clean_local_db import should_demote  # noqa: E402


def row(**kw) -> dict:
    """一条能通过投递池判定的最小 jobs 行（缺 JD，所以是补详情的候选）。"""
    base = {
        "canonical_id": "boss:x", "title": "AI Agent 开发工程师",
        "company_name": "示例科技", "city": "杭州",
        "salary_text": "18-24K", "salary_min_k": 18.0, "salary_max_k": 24.0,
        "salary_months": 12, "experience": "经验不限", "education": "本科",
        "job_invalid": 0, "job_status_desc": "", "is_proxy_job": 0,
        "recruiter_title": "HR", "raw_json": "{}", "job_description": "",
        "last_seen_at": date.today().isoformat(),
    }
    base.update(kw)
    return base


def test_backfill_reuses_the_single_demotion_predicate():
    """补详情必须复用 clean_local_db 的判定，不能另写一套阈值。

    两份判定会各自漂移，而漂移的代价是「抓了却被降级」——那是已经花掉的
    请求，事后无法退回。
    """
    assert B.should_demote is should_demote


def test_baseline_row_is_worth_fetching():
    """基准行本身必须通过，否则下面的用例证明不了任何事。"""
    demote, why = should_demote(row())
    assert not demote, f"基准行不该被降级，却因 {why!r} 被拦"


def test_over_salary_row_is_skipped_before_spending_quota():
    """薪资超纲是补详情候选里最大的一类（实测 1332 条），必须在花配额前拦掉。

    判据是区间**均值**（见 profile.salary_out_of_range）：`30-40K` 均值 35
    远超 25K 上限，抓了详情也一定被降级。
    """
    demote, why = should_demote(row(salary_min_k=30.0, salary_max_k=40.0,
                                    salary_text="30-40K"))
    assert demote and "超纲" in why


def test_anonymous_company_row_is_skipped_before_spending_quota():
    """匿名公司投递前无法核实身份，一律不进池（实测 778 条）。

    这些是采集侧加匿名拦截**之前**入库的存量，`_should_keep` 追溯不到它们。
    """
    demote, why = should_demote(row(raw_json='{"anonymous": 1}'))
    assert demote and "匿名" in why


def test_off_whitelist_city_row_is_skipped_before_spending_quota():
    """非目标城市（实测 129 条）同样注定降级。"""
    demote, why = should_demote(row(city="北京"))
    assert demote and "非目标城市" in why


def test_rechecks_each_row_immediately_before_spending_the_request():
    """每条抓取前必须重读该行再判一次，且重判要在发请求**之前**。

    候选列表是启动时一次性取完的，而 1000 条要跑 3.5 小时 —— 这期间库里的
    状态完全可能已经变（另一进程跑了 clean_local_db、列表轮次更新了薪资/城市）。
    实测 2026-08-29 本轮 273 条里 45 条注定被降级，其中 27 条的薪资和城市
    在列表阶段就已知，而选行那一刻它们还是 active。

    顺序错了不会报错，只是保护静默失效 —— 所以这里按源码位置钉死。
    """
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "backfill_details.py").read_text(encoding="utf-8")
    recheck = src.index("should_demote(fresh)")
    request = src.index("await collector._collect_detail(job)")
    assert recheck < request, "重判必须在发详情请求之前，否则配额已经花掉了"
    # 重读的必须是当前那一行，不能沿用启动时的快照
    assert "WHERE canonical_id = ?" in src


def test_skips_rows_another_process_already_filled():
    """别的进程已补上 JD 的行要跳过，不重复花配额。

    两个补详情进程会串行（Chrome SingletonLock），但 sustained_run 的交替模式
    和手工重跑都可能让同一行被排进两轮的候选里。
    """
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "backfill_details.py").read_text(encoding="utf-8")
    assert 'fresh["job_description"]' in src
