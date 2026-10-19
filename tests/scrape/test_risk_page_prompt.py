"""从风控页读平台原话，并用它精确归类。

为什么值得单独测：URL 和字节数只能判出「大概是哪一类」，判不出「这次为什么」。
页面本来就写着原因 —— `您的IP地址存在异常行为`、`访问过于频繁，请稍后再试`。
这些原句比分类标签更精确，也直接告诉人下一步该做什么（换出口？过滑块？等冷却？）。

2026-10-19 之前只看 URL + 字节数，把一个 1KB 的「IP 地址存在异常」页
归成了 `empty_page`（当成我们自己的 bug）。
"""
from __future__ import annotations

import pytest

from job_radar.collectors.boss import _page_prompt, _prompt_verdict, _ssr_blank_reason

URL = "https://www.zhipin.com/job_detail/abc.html"


def test_extracts_platform_message_from_verify_page():
    html = "<html><body><div>您的账号可能存在异常访问行为，完成验证后即可正常使用</div></body></html>"
    assert _page_prompt(html) == "您的账号可能存在异常访问行为，完成验证后即可正常使用"


def test_extracts_ip_restriction_message():
    html = "<html><p>您的IP地址存在异常行为，请稍后再试</p></html>"
    assert "IP地址存在异常" in _page_prompt(html)


def test_strips_script_and_style():
    """风控页的脚本里也常有这些词，必须只取可见文本。"""
    html = "<html><script>var msg='账户存在异常'</script><style>.a{}</style><p>访问过于频繁</p></html>"
    assert _page_prompt(html) == "访问过于频繁"


def test_returns_empty_on_normal_job_page():
    """正常岗位页的首句是 JD 正文，不能当成「平台提示」。"""
    html = '<html><div class="job-sec-text">负责 AI Agent 开发</div></html>'
    assert _page_prompt(html) == ""


@pytest.mark.parametrize("prompt,category,code", [
    ("您的IP地址存在异常行为", "network_restriction", 35),
    ("您的账户存在异常行为", "account_restriction", 36),
    ("您的环境存在异常", "environment_restriction", 37),
    ("访问过于频繁，请稍后再试", "rate_limited", None),
    ("请先登录后再查看", "authentication_required", 7),
])
def test_prompt_maps_to_category(prompt, category, code):
    assert _prompt_verdict(prompt) == (category, code)


def test_unknown_prompt_returns_none():
    assert _prompt_verdict("负责 AI Agent 开发") is None
    assert _prompt_verdict("") is None


def test_short_page_with_ip_message_is_risk_not_empty():
    """1KB 的「IP 地址存在异常」页必须判成风控，不能判成自己的 bug。

    这是文案判据最大的价值：字节数分档对短页毫无分辨力。
    """
    html = "<html><p>您的IP地址存在异常行为，请稍后再试</p></html>"
    diag = _ssr_blank_reason(html, URL, 200)
    assert diag["kind"] == "ip_restricted"
    assert diag["category"] == "network_restriction"
    assert diag["code"] == 35
    assert diag["evidence"] == "page_prompt"


def test_prompt_takes_priority_over_url_patterns():
    """页面文案优先于 URL 模式：地址没变但内容说 IP 被限，按 IP 限处理。"""
    html = "<html><p>您的IP地址存在异常行为</p></html>"
    diag = _ssr_blank_reason(
        html, "https://www.zhipin.com/web/passport/zp/security.html?s=1", 200)
    assert diag["kind"] == "ip_restricted"


def test_clean_challenge_page_still_falls_back_to_url_and_size():
    """没有文案时退回原有判据，行为不变。"""
    challenge = _ssr_blank_reason(
        "<html>" + "x" * 43000 + "</html>",
        "https://www.zhipin.com/web/passport/zp/security.html?s=1", 200)
    assert challenge["kind"] == "js_challenge"
    verify = _ssr_blank_reason(
        "<html>" + "x" * 1000 + "</html>",
        "https://www.zhipin.com/web/passport/zp/verify.html", 200)
    assert verify["kind"] == "verification"
