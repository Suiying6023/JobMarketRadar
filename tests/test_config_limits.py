"""Configuration contracts, independent of private calibration files.

A previously successful batch is not a known-safe platform quota. Tests verify
operator limits and validation, not inferred relationships between code37 and bans.
"""
from pathlib import Path
import pytest
from job_radar.config import load_config
from job_radar.models import CollectionConfig

ROOT = Path(__file__).resolve().parent.parent

@pytest.mark.parametrize('name', ['config.example.yaml', 'config.yaml', 'config.hangzhou.yaml', 'config.smoke.yaml'])
def test_distributed_examples_are_offline(name):
    cfg = load_config(ROOT / name)
    assert not cfg.network_enabled
    assert cfg.budget_mode == 'reference'
    assert not cfg.auto_chat.enabled
    assert cfg.auto_chat.dry_run
    assert cfg.over_quota_action == 'stop'
    assert cfg.page_size <= 30
    assert 1 <= cfg.max_requests_per_run <= cfg.max_requests_per_day


def test_per_keyword_depth_is_explicit_and_validated():
    cfg = CollectionConfig(cities=[{'name': '杭州'}], keywords=['Python', 'RAG'],
                           pages_per_query=1, pages_per_keyword={'RAG': 2})
    assert cfg.pages_for('RAG') == 2
    assert cfg.pages_for('Python') == 1
    with pytest.raises(ValueError):
        CollectionConfig(cities=[{'name': '杭州'}], keywords=['Python'], pages_per_keyword={'typo': 2})


def test_city_codes_are_resolved_without_live_lookup():
    cfg = CollectionConfig(cities=[{'name': name} for name in ['杭州', '上海', '深圳']], keywords=['Python'])
    assert len({c.boss_code for c in cfg.cities}) == 3
    assert all(c.boss_code for c in cfg.cities)
