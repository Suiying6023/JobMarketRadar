"""模型判页：规则认不出来的异常页，交给决策模型。

## 为什么要有这一层

2026-10-20 的事故里，规则的四层判据全部漏掉同一个封禁页（URL 不认识 403.html、
字节数落进挑战页区间、文案模式要求「地址」二字、code 编号被第一个块带偏）。
每补一次规则就要等下一次事故来验证 —— 这和 JD 判定那边已经放弃的做法一样。

**分工**：规则负责它擅长的（`code-block` 的 `show` 标记、`expire-time` 这种
机械提取），模型负责「这是什么页」这种语义判断。规则先跑，认不出来才叫模型。

这里用假 client 测，不联网。
"""
from __future__ import annotations

import pytest

from job_analysis.page_state import (
    PAGE_QUESTIONS,
    PAGE_STATE_ACTION,
    PAGE_STATE_CATEGORY,
    PAGE_STATE_OPTIONS,
    build_page_state,
    classify_page,
)


class FakeClient:
    def __init__(self, choice, confidence=0.9):
        self.choice = choice
        self.confidence = confidence
        self.calls = []
        self.state = None

    def decide(self, state, questions):
        self.calls.append((state, questions))
        self.state = state
        return {"page_state": {"type": "choice", "choice": self.choice,
                               "confidence": self.confidence,
                               "probabilities": {self.choice: self.confidence}}}


class BoomClient:
    def decide(self, state, questions):
        raise RuntimeError("模型挂了")


def test_state_contains_only_visible_text_and_metadata():
    state = build_page_state("  您好世界  ", url="https://x/y", size=123, http_status=200)
    assert state["visible_text"] == "您好世界"
    assert state["url"] == "https://x/y"
    assert state["byte_size"] == 123
    assert state["http_status"] == 200
    assert state["text_length"] == 4


def test_state_truncates_long_text():
    state = build_page_state("x" * 9999)
    assert len(state["visible_text"]) == 6000
    assert state["text_truncated"] is True


def test_options_cover_non_risk_cases():
    """选项里必须有「不是风控」的几项，否则模型会把正常下线硬塞进风控类。"""
    for name in ("normal_content", "job_closed", "job_not_found"):
        assert name in PAGE_STATE_OPTIONS
        assert PAGE_STATE_ACTION[name] in ("skip", "continue")


@pytest.mark.parametrize("state,action", [
    ("account_restricted", "stop"),
    ("ip_restricted", "stop"),
    ("verification_required", "stop"),
    ("rate_limited", "stop"),
    ("login_required", "stop"),
    ("job_closed", "skip"),
    ("job_not_found", "skip"),
])
def test_action_mapping(state, action):
    assert PAGE_STATE_ACTION[state] == action


def test_classify_returns_state_action_category():
    result = classify_page(FakeClient("account_restricted"), "您的账户存在异常行为")
    assert result["state"] == "account_restricted"
    assert result["action"] == "stop"
    assert result["category"] == "account_restriction"
    assert result["confidence"] == 0.9


def test_classify_rejects_choice_outside_options():
    """模型回了选项外的值 → unknown，**不能**当成 normal_content。"""
    result = classify_page(FakeClient("some_new_thing"), "随便一页")
    assert result["state"] == "unknown"
    assert result["action"] == "stop"


def test_classify_propagates_error():
    """模型失败必须抛出去 —— 调用方要退回规则，而不是当作页面正常。"""
    with pytest.raises(RuntimeError):
        classify_page(BoomClient(), "随便一页")


def test_questions_declare_choice_type():
    q = PAGE_QUESTIONS["page_state"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == set(PAGE_STATE_OPTIONS)


def test_every_category_is_known_to_collector():
    """`boss._JUDGE_KIND` 必须覆盖所有状态，否则模型判出来也没人认。"""
    from job_radar.collectors.boss import _JUDGE_KIND

    missing = set(PAGE_STATE_OPTIONS) - set(_JUDGE_KIND)
    assert not missing, f"这些状态没有映射到 kind：{missing}"


def test_categories_align_with_response_condition():
    """category 词汇要和 `boss_payload.response_condition` 一致，便于统一处理。"""
    allowed = {"", "account_restriction", "network_restriction",
               "environment_restriction", "authentication_required",
               "verification_required", "rate_limited", "page_changed", "unknown"}
    assert set(PAGE_STATE_CATEGORY.values()) <= allowed
