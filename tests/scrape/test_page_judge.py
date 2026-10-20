"""规则先跑、模型兜底：确认判页的调用时机正确。

## 为什么这条要单独测

如果模型在**每个**空正文页面都被调用，成本会失控（正常下线的岗位很多）。
反过来，如果模型从来不被调用，那这一层就是摆设。

更要紧的是第三种错误：**规则已经判出风控了，却又被模型的 `normal_content`
覆盖**。规则能拿到 `code-block` 的 `show` 标记和 `expire-time`，这些是模型
看不到的结构信息 —— 模型不该推翻它。
"""
from __future__ import annotations

from job_radar.collectors.boss import _ssr_blank_reason

URL = "https://www.zhipin.com/job_detail/abc.html"


class SpyJudge:
    """记录调用次数，并按预设返回。"""

    def __init__(self, state="unknown"):
        self.state = state
        self.calls = 0

    def __call__(self, text, url, size, status):
        self.calls += 1
        return {"state": self.state, "action": "stop", "category": "unknown",
                "confidence": 0.8}


def blocked_html(code=32, expire="2026-10-21 00:01"):
    return (f'<html><div id="code{code}" class="code-block show">'
            f'<p>您的账户存在异常行为，已暂时被限制访问！</p>'
            f'<p>将于 <span class="expire-time">{expire}</span> 恢复正常</p></div></html>')


def test_judge_not_called_when_rule_already_knows():
    """封禁页由规则精确判出（含 show 块和 expire），不该再花模型调用。"""
    judge = SpyJudge("normal_content")
    diag = _ssr_blank_reason(blocked_html(), "https://www.zhipin.com/web/passport/zp/403.html?code=32",
                             200, judge=judge)
    assert judge.calls == 0
    assert diag["kind"] == "account_restricted"
    assert diag["expire_at"] == "2026-10-21 00:01"


def test_judge_not_called_when_rule_sees_closed_mark():
    """正常下线的岗位很多，不该每个都问模型。"""
    judge = SpyJudge()
    diag = _ssr_blank_reason("<html><p>该职位已关闭</p></html>", URL, 200, judge=judge)
    assert judge.calls == 0
    assert diag["kind"] == "closed"


def test_judge_not_called_when_jd_container_exists():
    """有正文容器就不必判页。"""
    judge = SpyJudge()
    diag = _ssr_blank_reason('<html><div class="job-sec-text"></div></html>', URL, 200,
                             judge=judge)
    assert judge.calls == 0
    assert diag["kind"] == "blank_jd"


def test_judge_called_on_unrecognized_page():
    """不认识的页面（短、无关键字、不在挑战页字节区间）→ 交给模型。"""
    judge = SpyJudge("account_restricted")
    diag = _ssr_blank_reason("<html><p>您的账户已被限制</p></html>", URL, 200, judge=judge)
    assert judge.calls == 1
    assert diag["kind"] == "account_restricted"
    assert diag["evidence"] == "llm:account_restricted"


def test_judge_can_reclassify_as_closed():
    """模型判出「岗位已下线」时，不该当成风控停机。"""
    judge = SpyJudge("job_closed")
    diag = _ssr_blank_reason("<html><p>嗯</p></html>", URL, 200, judge=judge)
    assert diag["kind"] == "closed"


def test_judge_can_report_page_changed():
    """版式变了 → selector_miss，这是我们自己的 bug，要能看出来。"""
    judge = SpyJudge("page_changed")
    diag = _ssr_blank_reason("<html><p>嗯</p></html>", URL, 200, judge=judge)
    assert diag["kind"] == "selector_miss"


def test_judge_failure_falls_back_to_rules():
    """模型挂了必须退回规则判据，**不能**当作页面正常放行。"""
    def boom(text, url, size, status):
        raise RuntimeError("模型挂了")

    diag = _ssr_blank_reason("<html><p>嗯</p></html>", URL, 200, judge=boom)
    # 退回字节数兜底：小页面 → empty_page（不是「正常」）
    assert diag["kind"] in ("empty_page", "selector_miss")
    assert diag["evidence"] == "no-container"


def test_judge_returning_unknown_choice_does_not_claim_normal():
    """模型回了个不认识的状态 → 不采信，退回规则。"""
    judge = SpyJudge("这不是任何选项")
    diag = _ssr_blank_reason("<html><p>嗯</p></html>", URL, 200, judge=judge)
    assert diag["kind"] in ("empty_page", "selector_miss")


def test_no_judge_keeps_old_behaviour():
    """没注入 judge 时行为与之前完全一致。"""
    diag = _ssr_blank_reason("<html>" + "x" * 43000 + "</html>", URL, 200)
    assert diag["kind"] == "js_challenge"
