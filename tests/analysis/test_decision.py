"""分析层测试：决策模型客户端的重试语义 + 标签摊平。

重点测**会出错的地方**，不测"函数能跑"：
  · 503 要重试，400 不许重试（重试 400 只会白白打上游）
  · 重试次数用完要抛，不能静默返回空
  · 并发必须每线程独立 opener —— 共用会卡死（实测过，45 秒 1 条）
  · 标签摊平后类型要对，缺字段要变 None 而不是 KeyError
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from job_analysis.decision import DecisionClient, DecisionError, run_batch
from job_analysis.schema import build_state, flatten

# ---------------------------------------------------------------- 假 opener


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _ScriptedOpener:
    """按脚本依次返回结果：dict → 成功响应；异常实例 → 抛出。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if not self.script:
            raise AssertionError("没有更多预设响应")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(json.dumps(item).encode("utf-8"))


def make_client(script: list, *, retries: int = 3) -> DecisionClient:
    client = DecisionClient(base_url="https://example.invalid/v1", api_key="k",
                            max_retries=retries)
    client._local.opener = _ScriptedOpener(script)
    return client


def answers() -> dict:
    return {"answers": {"role": {"type": "choice", "choice": "ai_app",
                                 "confidence": 0.9,
                                 "probabilities": {"ai_app": 0.9, "other": 0.1}}}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("job_analysis.decision.time.sleep", lambda *_: None)


# ---------------------------------------------------------------- 重试语义


def test_success_returns_answers():
    client = make_client([answers()])
    result = client.decide({"title": "x"}, {"role": {"type": "noul"}})
    assert result["role"]["choice"] == "ai_app"
    assert client.calls == 1


def test_503_is_retried_then_succeeds():
    error = HTTPError("u", 503, "unavailable", {}, io.BytesIO(b"busy"))
    client = make_client([error, answers()])
    result = client.decide({"title": "x"}, {"role": {"type": "noul"}})
    assert result["role"]["choice"] == "ai_app"
    assert client.calls == 2


def test_400_is_not_retried():
    """400 是请求本身有问题，重试只是再打一次上游。"""
    error = HTTPError("u", 400, "bad request", {}, io.BytesIO(b"invalid_request"))
    client = make_client([error, answers()])
    with pytest.raises(DecisionError) as excinfo:
        client.decide({"title": "x"}, {"role": {"type": "noul"}})
    assert excinfo.value.status == 400
    assert client.calls == 1


def test_retries_are_bounded():
    errors = [HTTPError("u", 503, "unavailable", {}, io.BytesIO(b""))] * 5
    client = make_client(errors, retries=2)
    with pytest.raises(DecisionError):
        client.decide({"title": "x"}, {"role": {"type": "noul"}})
    assert client.calls == 3          # 1 次初始 + 2 次重试


def test_connection_error_is_retryable():
    client = make_client([URLError("boom"), answers()])
    assert client.decide({}, {})["role"]["choice"] == "ai_app"
    assert client.calls == 2


def test_missing_answers_is_an_error_not_a_silent_empty():
    client = make_client([{"ok": True}], retries=0)
    with pytest.raises(DecisionError):
        client.decide({}, {})


def test_each_thread_gets_its_own_opener():
    """共用 OpenerDirector 在并发下会卡死（实测 14 并发 45 秒只完成 1 条）。"""
    client = DecisionClient(base_url="https://example.invalid/v1", api_key="k")
    first = client._opener
    second = client._opener
    assert first is second, "同一线程应复用"

    import threading
    seen = {}
    def worker():
        seen["opener"] = client._opener
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["opener"] is not first, "不同线程必须是不同 opener"


# ---------------------------------------------------------------- 批处理


def test_batch_isolates_single_failures():
    class FlakyClient:
        def decide(self, state, questions):
            if state.get("fail"):
                raise DecisionError("boom")
            return {"role": {"type": "noul", "noul": 0.5}}

    items = [{"id": 1}, {"id": 2, "fail": True}, {"id": 3}]
    results = list(run_batch(FlakyClient(), items, workers=3,
                             state_of=lambda item: item, questions={}))
    assert len(results) == 3
    ok = [r for r in results if r[2] is None]
    bad = [r for r in results if r[2] is not None]
    assert len(ok) == 2 and len(bad) == 1


# ---------------------------------------------------------------- 字段


def test_build_state_truncates_long_descriptions():
    state = build_state({"title": "t", "job_description": "字" * 7000})
    assert len(state["description"]) == 6000
    assert state["description_truncated"] is True


def test_build_state_keeps_title_for_mismatch_judgement():
    state = build_state({"title": "算法工程师", "job_description": "写测试用例"})
    assert state["title"] == "算法工程师"


def test_flatten_maps_every_question():
    flat = flatten({
        "role": {"choice": "ai_app", "confidence": 0.9, "probabilities": {"ai_app": 0.9}},
        "mismatch": {"noul": 0.8},
        "seniority": {"score": 2.4, "confidence": 0.7, "probabilities": {"2": 0.6}},
        "employment": {"choice": "fulltime", "confidence": 0.95},
        "fresh_grad": {"choice": "accept", "confidence": 0.8},
        "is_proxy": {"noul": 0.1},
    })
    assert flat["role_family"] == "ai_app"
    assert flat["title_mismatch"] == 0.8
    assert flat["seniority_score"] == 2.4
    assert flat["employment_type"] == "fulltime"
    assert flat["fresh_grad"] == "accept"
    assert flat["is_proxy"] == 0.1


def test_flatten_tolerates_missing_answers():
    flat = flatten({})
    assert flat["role_family"] is None
    assert flat["title_mismatch"] is None
    assert flat["is_proxy"] is None
