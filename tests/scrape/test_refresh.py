"""刷新链路判定测试。

两类：
  · `classify` —— 纯函数，把证据判成状态（这几条判据都是踩过坑才定下来的）
  · `read_job_page` / `parse_job_page` —— 把字节变成证据（解析在 Python 侧）

完整的「发请求 → 解析 → 判定 → 落库」在 `tests/e2e/test_refresh_e2e.py`，
这里只钉那些不该被浏览器掩盖的单点判据：

  · 判关闭看 `.job-status` 的**值**，不看节点空不空
  · 下架页（11KB）与挑战页（41KB）都是短页，但处置相反
  · 身份不符要判未判定，不能张冠李戴（降级不可逆）
  · 未知文案不猜
  · banner 里的状态不能被"相似职位"卡片顶掉
"""

from __future__ import annotations

from boss_scrape.refresh import (MIN_JOB_HTML_LEN, JobStatus, classify,
                                 parse_job_page, read_job_page, same_job)
from tests.support.pages import FILLER, job_page


def evidence(**overrides) -> dict:
    base = {"ok": True, "status": 200, "len": 145_000, "url": "https://www.zhipin.com/job_detail/x.html",
            "h1": "AI Agent 开发工程师", "job_status": "招聘中",
            "active_time": "今日活跃", "captcha_present": False,
            "restriction_notice": False}
    base.update(overrides)
    return base


def test_open_job_is_recognised():
    status = classify(evidence())
    assert status.state == "open"
    assert status.closed is False


def test_closed_job_is_recognised_by_value_not_by_emptiness():
    status = classify(evidence(job_status="职位已关闭"))
    assert status.state == "closed"
    assert status.closed is True


def test_empty_job_status_is_unknown_not_closed():
    """在招岗位该节点也可能没有文本；按"空即关闭"判会把全部岗位判成关闭。"""
    status = classify(evidence(job_status=""))
    assert status.state == "unknown"
    assert status.closed is None


def test_gone_page_is_gone_not_challenged():
    status = classify({"ok": False, "reason": "gone", "status": 404, "len": 11_000})
    assert status.state == "gone"


def test_challenge_page_is_challenged():
    status = classify({"ok": False, "reason": "challenged", "status": 200, "len": 41_000})
    assert status.state == "challenged"


def test_short_page_without_markers_is_unknown():
    status = classify({"ok": False, "reason": "short", "status": 200, "len": 20_000})
    assert status.state == "unknown"


def test_captcha_forces_challenged_even_on_200():
    status = classify(evidence(captcha_present=True))
    assert status.state == "challenged"


def test_restriction_notice_forces_challenged():
    status = classify(evidence(h1=None, restriction_notice=True))
    assert status.state == "challenged"


def test_http_429_is_challenged():
    status = classify(evidence(status=429))
    assert status.state == "challenged"


def test_identity_mismatch_is_unknown():
    """拿回来的不是请求的那条岗位时不能下结论——降级是单向的。"""
    status = classify(evidence(h1="完全不同的岗位标题"), title="AI Agent 开发工程师")
    assert status.state == "unknown"
    assert "身份不符" in status.reason


def test_unknown_status_text_is_not_guessed():
    status = classify(evidence(job_status="平台新加的文案"))
    assert status.state == "unknown"
    assert status.closed is None


def test_same_job_tolerates_suffix_differences():
    assert same_job("AI Agent 开发工程师（杭州）", "AI Agent 开发工程师")
    assert not same_job("产品经理", "AI Agent 开发工程师")
    assert not same_job("", "AI Agent 开发工程师")


def test_status_object_defaults_are_safe():
    status = JobStatus(state="unknown")
    assert status.closed is None
    assert status.raw == {}
    assert status.invalid is None


# ---------------------------------------------------------------- 取值（Python 侧）
# 页面由 `tests/support/pages.py` 提供，与端到端那套用同一份 —— 两处各写一份
# HTML 夹具，改了一边忘了另一边，测出来的就不是同一个结构了。


def test_parse_takes_the_banner_status_not_the_similar_job_card():
    parsed = parse_job_page(job_page("招聘中"))
    assert parsed["job_status"] == "招聘中"
    assert parsed["h1"] == "AI Agent 开发工程师"
    assert parsed["active_time"] == "今日活跃"
    assert parsed["captcha_present"] is False


def test_parse_falls_back_to_the_banner_h1_when_the_name_wrapper_is_gone():
    """平台改过一次结构：`.info-primary .name h1` 与 `.job-banner h1` 都实测出现过。"""
    html = ('<div class="job-banner"><div class="job-status">招聘中</div>'
            '<h1>后端开发工程师</h1></div>' + f'<div class="job-sec-text">{FILLER}</div>')
    assert parse_job_page(html)["h1"] == "后端开发工程师"


def test_parse_flags_a_captcha_widget_as_present():
    html = job_page("招聘中").replace(
        "<body>", '<body><div class="slider-verify"></div>')
    assert parse_job_page(html)["captcha_present"] is True


def test_read_splits_the_three_kinds_of_short_page():
    """三种短页面的处置完全相反，必须分开：下架→判关闭，挑战/验证→停机。"""
    def fetched(text: str, status: int = 200) -> dict:
        return {"ok": True, "status": status, "url": "https://www.zhipin.com/job_detail/x.html",
                "text": text}

    assert read_job_page(fetched("您访问的页面不存在" * 900, status=404))["reason"] == "gone"
    assert read_job_page(fetched("security-check 请稍候" * 2_000))["reason"] == "challenged"
    assert read_job_page(fetched("安全验证" * 4_000))["reason"] == "verify"
    assert read_job_page(fetched("无关内容" * 3_000))["reason"] == "short"


def test_read_marks_gone_and_challenged_for_the_task_ledger():
    """任务链路按 `evidence["gone"]` 判下架，旧取值脚本从来没给过这个键。"""
    gone = read_job_page({"ok": True, "status": 404, "url": "", "text": "页面不存在" * 900})
    assert gone["gone"] is True
    challenged = read_job_page({"ok": True, "status": 200, "url": "",
                                "text": "security-check" * 3_000})
    assert challenged["challenged"] is True


def test_read_of_a_full_page_produces_the_evidence_classify_needs():
    evidence = read_job_page({"ok": True, "status": 200,
                              "url": "https://www.zhipin.com/job_detail/x.html",
                              "text": job_page("招聘中")})
    assert evidence["ok"] is True
    assert evidence["len"] > MIN_JOB_HTML_LEN
    assert classify(evidence, title="AI Agent 开发工程师").state == "open"
