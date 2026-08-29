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


def test_should_keep_rejects_anonymous_before_detail_fetch():
    """匿名公司必须在列表阶段就拦掉。

    标成 active 就会去抓详情，而这类岗位最终一定被 clean_local_db 降级
    （投递前无法核实公司身份）——每条漏过去就白花一次详情配额。
    """
    collector = _collector()
    raw = json.loads(Path("tests/fixtures/boss_joblist.json").read_text(encoding="utf-8"))["zpData"]["jobList"][0]

    normal = collector._parse_api_job(raw, "AI Agent", 1)
    assert collector._should_keep(normal) is True

    anon = collector._parse_api_job({**raw, "anonymous": 101,
                                     "brandName": "某大型互联网公司"}, "AI Agent", 1)
    assert collector._should_keep(anon) is False
