"""Collection admission uses explicit reusable configuration."""
import pytest

from job_radar.collectors.boss import BossCollector
from job_radar.models import CollectionConfig, JobRecord


def collector(**overrides):
    return BossCollector(CollectionConfig.model_validate({
        "cities": [{"name": "全国"}], "keywords": ["工程师"], **overrides}))


def job(**values):
    base = dict(platform="boss", source_job_id="sample", job_url="https://www.zhipin.com/job_detail/sample.html",
                title="应用开发工程师", company_name="示例企业", city="杭州",
                experience="经验不限", education="本科", salary_min_k=15, salary_max_k=25, salary_text="15-25K")
    base.update(values)
    return JobRecord(**base)


@pytest.mark.parametrize("values", [
    {}, {"city": "成都"}, {"salary_min_k": 40, "salary_max_k": 60, "salary_text": "40-60K"},
    {"title": "2027届校招工程师"}, {"title": "Python开发（外包驻场）"},
    {"company_name": "人力资源服务公司"}, {"company_name": "某知名企业", "raw": {"anonymous": 1}},
])
def test_default_collection_has_no_hidden_personal_template(values):
    assert collector()._should_keep(job(**values))


def test_explicit_term_filters_apply_without_a_candidate():
    c = collector(include_title_keywords=["Agent", "大模型"], exclude_title_keywords=["销售"],
                  exclude_company_keywords=["劳务"])
    assert c._should_keep(job(title="AI Agent 开发"))
    assert not c._should_keep(job(title="大模型销售"))
    assert not c._should_keep(job(title="Agent 开发", company_name="示例劳务"))
    assert not c._should_keep(job(title="普通后端开发"))


def test_candidate_facts_are_opt_in_and_configurable():
    profile = {"cities": ["杭州"], "graduation_year": 2026, "salary_min_k": 10, "salary_max_k": 28}
    unfiltered = collector(candidate=profile)
    filtered = collector(candidate=profile, service={"candidate_filtering": True})
    assert unfiltered._should_keep(job(city="成都"))
    assert filtered._should_keep(job())
    assert not filtered._should_keep(job(city="成都"))
    assert not filtered._should_keep(job(salary_min_k=5, salary_max_k=8, salary_text="5-8K"))
    assert filtered._should_keep(job(salary_min_k=40, salary_max_k=60, salary_text="40-60K"))
    assert not filtered._should_keep(job(title="2027届校招工程师"))


def test_invalid_records_are_rejected_without_personal_rules():
    assert not collector()._should_keep(job(title=""))
    assert not collector()._should_keep(job(job_url=""))
