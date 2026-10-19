"""关键词产出判据的回归测试。

钉住 2026-08-11 的口径修正：**"独占产出为 0" 不等于 "该砍"**。
BOSS 搜索结果浅层各词共享、深层各词独有，一个只翻了 3 页的词独占产出必然接近 0,
那是采集深度不足，不是被别的词覆盖。历史误判：`Python开发工程师` 按页1-2 零新增
被判"该砍"，翻深后独占画像内 47 条。

这个判据只在"决定删关键词"的那一刻起作用，平时看不出坏没坏，所以必须有测试。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "analyze_keyword_yield.py"


def _load():
    """scripts/ 不是包（pythonpath 只含 src），按文件路径加载。"""
    spec = importlib.util.spec_from_file_location("analyze_keyword_yield", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_shallow_zero_exclusive_is_not_a_cut_recommendation():
    """核心修正：翻得浅 + 独占 0 → 只能说数据不足，不能建议砍。"""
    v = mod.verdict(in_config=True, rate=63.0, own_act=0, page=3, exhausted=0)
    assert "数据不足" in v
    assert "可砍" not in v


def test_deep_zero_exclusive_is_a_cut_recommendation():
    """翻透了还是没独占产出，才是真的被覆盖。"""
    v = mod.verdict(in_config=True, rate=45.0, own_act=0,
                    page=mod.DEEP_PAGE, exhausted=0)
    assert "可砍" in v


def test_exhausted_zero_exclusive_is_a_cut_recommendation():
    """空 jobList 确认翻到底，即使页数不深也可以判被覆盖。"""
    v = mod.verdict(in_config=True, rate=45.0, own_act=0, page=2, exhausted=1)
    assert "可砍" in v
    assert "数据不足" not in v


def test_low_hit_rate_cuts_regardless_of_depth():
    """跑题与深度无关：翻得再深也不会变准（AIGC 命中率 1.7% 的教训）。"""
    v = mod.verdict(in_config=True, rate=1.7, own_act=0, page=1, exhausted=0)
    assert "可砍" in v
    assert "数据不足" not in v


def test_exclusive_output_keeps_keyword():
    v = mod.verdict(in_config=True, rate=76.7, own_act=177, page=10, exhausted=0)
    assert "保留" in v


def test_keyword_absent_from_config_is_not_labelled_disabled_wrongly():
    """不在配置里的词标"未启用"，不该混进"可砍"建议。"""
    v = mod.verdict(in_config=False, rate=76.7, own_act=177, page=10, exhausted=0)
    assert v == "未启用"


def test_keyword_set_comes_from_the_selected_config(tmp_path):
    """配置选择是输入，测试不依赖用户的私有关键词档。"""
    path = tmp_path / "custom.yaml"
    path.write_text("cities:\n  - name: 杭州\nkeywords:\n  - 自定义岗位\n  - Python开发工程师\n", encoding="utf-8")
    assert mod.current_keywords(str(path)) == {"自定义岗位", "Python开发工程师"}
