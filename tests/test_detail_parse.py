"""详情接口解析测试。

fixture 由 2026-07-29 真机抓取的 /wapi/zpgeek/job/detail.json 响应裁剪而来，
只保留解析用到的字段。这样改代码时不需要连真站也能验证解析逻辑。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from job_radar.collectors.boss import BossCollector
from job_radar.models import CollectionConfig, JobRecord, Platform
from job_radar.normalization import active_days_from_desc

FIXTURE = Path(__file__).parent / "fixtures" / "boss_job_detail.json"


def make_job() -> JobRecord:
    return JobRecord(
        platform=Platform.BOSS,
        source_job_id="21f952c6631e862c0nJ63NS_EFJS",
        job_url="https://www.zhipin.com/job_detail/21f952c6631e862c0nJ63NS_EFJS.html",
        title="AI Agent 工程师",
        raw={"securityId": "fake-security-id", "lid": "fake-lid"},
    )


def make_collector() -> BossCollector:
    cfg = CollectionConfig.model_validate({
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
        "detail_delay_seconds": [0, 0],
    })
    collector = BossCollector(cfg)
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch(_path: str, recover_code37: bool = False) -> dict:
        return body

    collector._same_origin_fetch = fake_fetch  # type: ignore[assignment]
    return collector


def test_detail_fills_jd_and_activity():
    job = asyncio.run(make_collector()._collect_detail(make_job()))
    # JD 来自 jobInfo.postDescription，是纯文本，不依赖任何 CSS class。
    assert "Agent核心开发" in job.job_description
    assert job.recruiter_active_status == "刚刚活跃"
    assert job.recruiter_active_days == 0
    assert job.recruiter_name == "示例HR"
    assert job.recruiter_title == "HR"


def test_detail_fills_company_and_flags():
    job = asyncio.run(make_collector()._collect_detail(make_job()))
    assert job.company_size == "100-499人"
    assert job.financing_stage == "未融资"
    assert job.address == "杭州示例区示例大厦1栋10层"
    # proxyJob/proxyType 都是 0，说明是直接雇主而非代招。
    assert job.is_proxy_job is False
    assert job.job_invalid is False
    assert "AI Agent" in job.skills
    assert "五险一金" in job.welfare


def test_detail_requires_security_id():
    """没有 securityId 时详情接口会被打回空白页，应提前失败而不是白发请求。"""
    job = make_job()
    job.raw = {}
    try:
        asyncio.run(make_collector()._collect_detail(job))
    except RuntimeError as exc:
        assert "securityId" in str(exc)
    else:
        raise AssertionError("缺少 securityId 时应当抛错")


def test_active_days_ordering():
    """活跃度要能比较，否则没法筛掉长期不上线的招聘者。"""
    assert active_days_from_desc("刚刚活跃") == 0
    assert active_days_from_desc("本周活跃") == 7
    assert active_days_from_desc("半年前活跃") == 180
    assert active_days_from_desc("刚刚活跃") < active_days_from_desc("半年前活跃")
    assert active_days_from_desc("") is None
