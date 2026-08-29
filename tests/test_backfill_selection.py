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
