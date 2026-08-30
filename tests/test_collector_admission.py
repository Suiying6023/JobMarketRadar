"""列表阶段准入判定测试（`BossCollector._should_keep`）。

**这是唯一花不可退配额的一环**：列表阶段放过一条，就意味着后面要为它花一次
详情请求，而详情请求发出去就退不回来。所以"列表能判的一定要在列表判"。

实测代价（2026-08-30）：收敛前有 3207 条注定被 clean_local_db 降级的岗位
被放过，其中 **828 条已经花掉详情配额**（超纲 596、匿名 112、公司名 69、
标题 30、非目标城市 10）。超纲一项占 596 条，而薪资字段全部来自列表响应 ——
纯粹是判据没写在该写的地方。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar import profile as P  # noqa: E402
from job_radar.collectors.boss import BossCollector  # noqa: E402
from job_radar.models import CollectionConfig, JobRecord  # noqa: E402


def collector(**overrides) -> BossCollector:
    payload = {
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        **overrides,
    }
    return BossCollector(CollectionConfig.model_validate(payload))


def job(**kw) -> JobRecord:
    base = dict(
        platform="boss", source_job_id="1", job_url="https://x/y",
        title="AI Agent 开发工程师", company_name="示例科技", city="杭州",
        salary_text="15-25K", salary_min_k=15.0, salary_max_k=25.0,
        salary_months=12, raw={},
    )
    base.update(kw)
    return JobRecord(**base)


def test_baseline_job_is_admitted():
    """基准行必须通过，否则下面的用例证明不了任何事。"""
    assert collector()._should_keep(job())


def test_salary_out_of_range_is_rejected_at_list_stage():
    """超纲是最大的一笔浪费（596 条已花配额），而薪资在列表里就已知。"""
    assert not collector()._should_keep(
        job(salary_text="40-60K", salary_min_k=40.0, salary_max_k=60.0))


def test_city_outside_whitelist_is_rejected_at_list_stage():
    """城市同样在列表响应里，不该等抓完详情才判。"""
    assert not collector()._should_keep(job(city="成都"))


def test_profile_blacklists_apply_even_when_config_lists_are_empty():
    """profile 是基线，config 只能**追加**不能替代。

    原先只读 config 的 YAML 名单，两份各自漂移：实测 profile 有而 YAML 缺
    `猎头`/`劳务派遣`（标题）、`人力资源`/`华钦`/`新致软件`/`汉得`（公司）。
    """
    c = collector(exclude_title_keywords=[], exclude_company_keywords=[])
    assert not c._should_keep(job(title="Python开发（外包驻场）"))
    assert not c._should_keep(job(company_name="某某人力资源服务有限公司"))
    assert not c._should_keep(job(company_name="新致软件股份"))


def test_config_lists_still_add_on_top_of_profile():
    """config 追加的词要照旧生效（按轮次临时加词的能力不能丢）。"""
    c = collector(exclude_title_keywords=["区块链"])
    assert not c._should_keep(job(title="区块链开发工程师"))


def test_od_uses_word_boundary_so_coding_is_not_eaten():
    """OD 是外包标记，但裸子串会吃掉 Coding —— 这里必须走词边界判定。"""
    c = collector()
    assert not c._should_keep(job(title="华为云数据库Java开发工程师（OD）"))
    assert c._should_keep(job(title="AI Coding Agent 研发工程师"))


def test_future_cohort_is_rejected_at_list_stage():
    """届数只在标题里。实测 8 条 27 届岗位曾以 active 留在库里被反复刷状态。"""
    assert not collector()._should_keep(job(title="AI应用开发工程师【27届校招】"))
    assert collector()._should_keep(job(title="AI开发工程师【26届校招】"))


def test_anonymous_company_is_rejected_at_list_stage():
    """投递前无法核实公司身份的岗位最终一定被降级，别为它花详情配额。"""
    assert not collector()._should_keep(
        job(company_name="某知名互联网公司", raw={"anonymous": 1}))


def test_include_keywords_still_gate_admission():
    """include 名单存在时，不含任一关键词的岗位不该进来。"""
    c = collector(include_title_keywords=["Agent", "大模型"])
    assert c._should_keep(job(title="AI Agent 开发工程师"))
    assert not c._should_keep(job(title="前端开发工程师"))


def test_admission_matches_the_downstream_demotion_criteria():
    """列表准入与下游淘汰必须同源 —— 差集就是纯浪费。

    这条钉的是**机制**而非某个词：`_should_keep` 要调用 profile 的判据函数，
    不能自己另写一套阈值（两份会各自漂移，而漂移的代价是已经花掉的配额）。
    """
    import inspect
    src = inspect.getsource(BossCollector._should_keep)
    for fn in ("EXCLUDE_TITLE", "EXCLUDE_COMPANY", "city_allowed",
               "salary_out_of_range", "outsourced_od", "future_cohort",
               "is_anonymous"):
        assert fn in src, f"_should_keep 没有复用 profile.{fn}"
