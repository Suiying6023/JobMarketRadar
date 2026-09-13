from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_radar.analysis.decisions import evaluate, mentions, safe_job_url
from job_radar.analysis.market import MarketSnapshot, csv_export
from job_radar.candidate import CandidateProfile
from job_radar.models import CollectionConfig, JobRecord, Platform
from job_radar.settings import RevisionConflict, SettingsStore, init_config
from job_radar.storage import JobStore

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)
PROFILE = CandidateProfile(cities=["杭州", "上海", "深圳"], preferred_cities=["杭州", "上海", "深圳"],
                           graduation_year=2026, roles=["Agent", "大模型"], skills=["Python"], salary_min_k=10, salary_max_k=28)


def job(**values):
    return dict(title="AI Agent开发工程师", company_name="示例软件", city="杭州", job_description="参与 Agent 应用开发，使用 Python 完成服务。",
                experience="经验不限", education="本科", salary_min_k=12, salary_max_k=20, salary_text="12-20K", detail_fetched_at="2026-09-12T00:00:00Z", **values)


def changed(**values):
    row = job()
    row.update(values)
    return row


def test_old_keep_label_does_not_waive_explicit_experience():
    row = changed(job_description="必须有3年以上后端开发经验，熟悉 Agent 工具优先。", glm_tags_json='{"match":"keep"}')
    result = evaluate(row, PROFILE, NOW)
    assert result["bucket"] == "暂不优先"
    assert any("3年" in reason for reason in result["concerns"])


def test_optional_experience_is_not_a_hard_requirement():
    result = evaluate(changed(job_description="负责 Python Agent 开发。具备3年以上相关经验者优先，不是必须条件。"), PROFILE, NOW)
    assert result["bucket"] == "优先核实"


def test_mixed_internship_is_flagged_instead_of_assuming_full_time():
    result = evaluate(changed(job_description="参与 Agent 项目开发，欢迎应届生。实习生每周实习4天，可转正。"), PROFILE, NOW)
    assert result["bucket"] == "需核实门槛"


def test_negative_graduate_phrase_cannot_waive_seniority():
    result = evaluate(changed(experience="3-5年", job_description="Agent开发。不接受应届生，必须有3年相关经验。"), PROFILE, NOW)
    assert result["bucket"] == "暂不优先"
    assert not any("明确提供应届" in r for r in result["reasons"])


def test_multiple_cohorts_and_equal_priority_cities():
    row = changed(title="26/27届 AI Agent开发工程师", job_description="负责大模型应用，欢迎应届毕业生。")
    scores = [evaluate({**row, "city": city}, PROFILE, NOW) for city in PROFILE.preferred_cities]
    assert all(r["bucket"] == "优先核实" for r in scores)
    assert len({r["score"] for r in scores}) == 1


def test_short_terms_do_not_match_arbitrary_substrings():
    assert not mentions("storage and coverage", ["RAG"])
    assert mentions("生产级 RAG 服务", ["RAG"])
    assert mentions("⼤模型与⼯作流", ["大模型", "工作流"])


def test_boss_tilde_identifiers_keep_their_review_link():
    valid = "https://www.zhipin.com/job_detail/example_123~.html"
    assert safe_job_url(valid) == valid
    assert safe_job_url("javascript:alert(1)") == ""
    assert safe_job_url("https://www.zhipin.com.evil.invalid/job_detail/1.html") == ""


def test_maximum_experience_and_graduation_wording_are_not_reversed():
    result = evaluate(changed(experience="1年以内", job_description="开发 Agent。应届毕业生或具备2年以内研发经验。"), PROFILE, NOW)
    assert result["bucket"] == "优先核实"
    later = evaluate(changed(job_description="面向2027年应届毕业生，开发 Agent。"), PROFILE, NOW)
    assert later["bucket"] == "暂不优先"


def test_nationwide_denominator_is_independent_of_candidate_filter(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with JobStore(path) as store:
        for i, city, description in [(1, "杭州", "Python Agent RAG"), (2, "北京", "Python"), (3, "深圳", "")]:
            store.upsert(JobRecord(platform=Platform.BOSS, source_job_id=str(i), title="工程师", city=city,
                                   job_url=f"https://www.zhipin.com/job_detail/{i}.html", job_description=description,
                                   salary_text="10-20K", salary_min_k=10, salary_max_k=20))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    snap = MarketSnapshot(path, NOW)
    report = snap.overview(PROFILE)
    assert report["totals"]["jobs"] == 3
    assert report["totals"]["with_jd"] == 2
    assert next(s for s in report["skills"] if s["name"] == "Python") == {"name": "Python", "count": 2, "denominator": 2, "percent": 100.0}
    assert len(snap.select(PROFILE, city="北京")) == 1
    assert sum(s["count"] for s in report["candidate_funnel"]) == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_database_never_creates_empty_file(tmp_path):
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(FileNotFoundError):
        MarketSnapshot(path)
    assert not path.exists()


def test_config_revision_validation_and_backup(tmp_path):
    path = init_config(tmp_path / "config.local.yaml")
    store = SettingsStore(path)
    cfg, rev = store.read()
    payload = cfg.model_dump(mode="json")
    payload["keywords"] = ["Python", "Python", " Agent "]
    saved, new_rev = store.save(payload, rev)
    assert saved.keywords == ["Python", "Agent"]
    assert new_rev != rev
    assert list((tmp_path / ".config-history").glob("*.yaml"))
    with pytest.raises(RevisionConflict):
        store.save(payload, rev)
    payload["request_delay_seconds"] = [10, 1]
    with pytest.raises(ValueError):
        store.save(payload, new_rev)
    assert store.read()[1] == new_rev


def test_workbench_cannot_enable_online_or_change_database(tmp_path):
    path = init_config(tmp_path / "config.local.yaml")
    store = SettingsStore(path)
    cfg, rev = store.read()
    for change in ({"network_enabled": True}, {"database_path": "elsewhere.sqlite3"}, {"budget_mode": "unknown"}):
        with pytest.raises(ValueError):
            store.save({**cfg.model_dump(mode="json"), **change}, rev)
    assert store.read()[1] == rev


def test_config_rejects_typos_nonfinite_delays_and_empty_plan():
    base = {"cities": [{"name": "杭州"}], "keywords": ["Python"]}
    for changes in ({"max_requsts_per_day": 1}, {"request_delay_seconds": [float("nan"), 10]}, {"keywords": []}):
        with pytest.raises(ValueError):
            CollectionConfig.model_validate({**base, **changes})


def test_csv_text_cannot_execute_formula():
    item = {"id": "boss:1", "company": '=HYPERLINK("https://invalid")', "title": "+SUM(1,2)", "city": "杭州", "salary": "12-20K",
            "experience": "经验不限", "education": "本科", "detail_fetched_at": "", "url": "",
            "decision": {"bucket": "优先核实", "score": 80, "reasons": [], "concerns": []}}
    exported = csv_export([item]).decode("utf-8-sig")
    assert "'=HYPERLINK" in exported
    assert "'+SUM" in exported
