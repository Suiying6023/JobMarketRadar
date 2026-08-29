"""配置自洽性测试：code37 熔断必须比日线宽。

2026-08-11 实测：code=37 是无记忆配额窗口，每约 7.1 次请求必触发一次
（1025 次请求 / 145 次 code=37）。所以 `code37_limit` 换算出来的是
**单次调用能发多少请求**，它必须大于 `max_requests_per_day`，
否则熔断会抢在日线之前把整轮掐断。

真实事故（08-11 03:33）：`backfill_details.py` 里残留硬编码 `code37_limit = 35`
（遮蔽了配置值），等于把单次调用卡在约 247 次请求 —— 当晚日线 1800 还剩一大截，
却在补完 170 条详情时就停了。修完硬编码后又发现 `cities3` 的 200 仍不够 1800。

这类问题平时完全看不出来：日志只说"超阈值，主动停止防封禁"，看着像在保护账号，
实际是配置不自洽。必须有测试钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from job_radar.config import load_config

ROOT = Path(__file__).resolve().parent.parent
# 每次 code=37 之间的请求数（实测下界，取保守值：越小越要求 code37_limit 大）
REQUESTS_PER_CODE37 = 5.0

CONFIGS = ["config.hangzhou.yaml", "config.steady.yaml",
           "config.cities3.yaml", "config.aggressive.yaml"]


@pytest.mark.parametrize("name", CONFIGS)
def test_code37_limit_wider_than_day_cap(name):
    """熔断换算出的请求数必须 ≥ 日线，否则熔断先于日线触发。

    hangzhou 是刻意的保守档（日线 600、熔断 35），它的语义就是"宁可早停"，
    所以豁免——但其余档位都是为了跑满日线而存在的，必须自洽。
    """
    cfg = load_config(name)
    reachable = cfg.code37_limit * REQUESTS_PER_CODE37
    if name == "config.hangzhou.yaml":
        pytest.skip("保守档刻意让熔断早于日线触发")
    assert reachable >= cfg.max_requests_per_day, (
        f"{name}: code37_limit={cfg.code37_limit} 换算约 {reachable:.0f} 次请求，"
        f"小于日线 {cfg.max_requests_per_day} → 熔断会抢在日线前掐断整轮。"
        f"至少需要 {int(cfg.max_requests_per_day / REQUESTS_PER_CODE37) + 1}")


def test_backfill_details_reads_config_not_hardcoded():
    """回归：backfill_details 里不能再出现硬编码的 code37 阈值。

    原来那行 `code37_limit = 35` 是局部变量，**遮蔽**了 collector 已经读好的
    配置值，而且不会报错——只会在跑到一半时安静地停下来。
    """
    src = (ROOT / "scripts" / "backfill_details.py").read_text(encoding="utf-8")
    assert "code37_limit = cfg.code37_limit" in src
    assert "code37_limit = 35" not in src


def test_backfill_activity_reads_config_not_hardcoded():
    src = (ROOT / "scripts" / "backfill_activity.py").read_text(encoding="utf-8")
    assert "cfg.code37_limit" in src
    assert "code37_count >= 35" not in src


def test_cities3_actually_has_three_cities():
    """`expand_list.py` 曾只读 cities[0]，静默跑三倍时间抓同一批数据。
    配置这端至少要保证三个城市都在，且都补到了 boss_code。"""
    cfg = load_config("config.cities3.yaml")
    names = [c.name for c in cfg.cities]
    assert names == ["杭州", "上海", "深圳"]
    assert all(c.boss_code for c in cfg.cities), "城市码未自动补全"
    assert len({c.boss_code for c in cfg.cities}) == 3, "城市码重复"


@pytest.mark.parametrize("name", CONFIGS)
def test_pages_per_keyword_keys_exist_in_keywords(name):
    """按词翻页深度的 key 必须真的在 keywords 里。

    拼写不一致（半角/全角、大小写、多余空格）不会报错，只会让那条覆盖
    静默失效 —— 配了 `AI agent: 2` 而列表里是 `AI Agent`，结果它照旧翻满
    默认页数，而日志里看不出任何异常。所以在配置加载阶段就拦。
    """
    cfg = load_config(name)
    unknown = set(cfg.pages_per_keyword) - set(cfg.keywords)
    assert not unknown, f"{name}: pages_per_keyword 有未知关键词 {unknown}"


def test_pages_for_falls_back_to_default():
    cfg = load_config("config.cities3.yaml")
    assert cfg.pages_for("python") == 15, "高对口率词应深翻"
    assert cfg.pages_for("AI Agent") == 2, "低对口率词应浅翻"
    assert cfg.pages_for("MCP") == cfg.pages_per_query, "未配置的词走默认"
    assert cfg.pages_for("不存在的词") == cfg.pages_per_query


def test_per_keyword_depth_costs_less_than_uniform_deep():
    """分词深度的意义是把配额从低对口词挪给高对口词，总量还要更省。"""
    cfg = load_config("config.cities3.yaml")
    per_kw = sum(cfg.pages_for(k) for k in cfg.keywords)
    uniform_deep = len(cfg.keywords) * 10
    assert per_kw < uniform_deep, "分词后总页数应低于统一深翻"
    # 高对口词拿到的深度必须严格大于低对口词
    assert cfg.pages_for("python") > cfg.pages_for("AI产品经理")
