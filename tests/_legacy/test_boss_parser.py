import json
from pathlib import Path

from job_radar.collectors.boss import BossCollector
from job_radar.models import CollectionConfig


def _collector() -> BossCollector:
    cfg = CollectionConfig.model_validate({
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
    })
    return BossCollector(cfg)


def test_parse_api_job():
    collector = _collector()
    raw = json.loads(Path("tests/fixtures/boss_joblist.json").read_text(encoding="utf-8"))["zpData"]["jobList"][0]
    job = collector._parse_api_job(raw, "AI Agent", 1)
    assert job.title == "AI Agent 开发工程师"
    assert job.company_name == "示例科技"
    assert job.salary_min_k == 20
    assert job.salary_months == 14
    assert job.recruiter_active_status == "在线"


def test_anonymous_source_fact_is_preserved_and_exclusion_is_explicit():
    """匿名是来源字段；通用采集由操作者选择是否排除。"""
    collector = _collector()
    raw = json.loads(Path("tests/fixtures/boss_joblist.json").read_text(encoding="utf-8"))["zpData"]["jobList"][0]
    raw = {**raw, "salaryDesc": "15-25K", "salaryMonth": 12}

    normal = collector._parse_api_job(raw, "AI Agent", 1)
    assert collector._should_keep(normal) is True, "基准行应通过，否则下面证明不了匿名判定"

    anon = collector._parse_api_job({**raw, "anonymous": 101,
                                     "brandName": "某大型互联网公司"}, "AI Agent", 1)
    assert anon.raw["anonymous"] == 101
    assert collector._should_keep(anon) is True
    collector.config.exclude_company_keywords = ["某大型互联网公司"]
    assert collector._should_keep(anon) is False
