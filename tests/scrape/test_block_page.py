"""封禁页（`403.html?code=N`）必须判成限制，不能判成 JS 挑战。

## 为什么值得单独测

2026-10-19 深夜到 2026-10-20 上午，平台一直在返回
`/web/passport/zp/403.html?code=32`，页面明写：

    您的账户存在异常行为，已暂时被限制访问！
    将于 2026-10-21 00:01 恢复正常
    请勿频繁提交刷新请求，耐心等待

但抓取链路把它判成了 `js_challenge`（「读早了，等 2 秒会自愈」），
于是我们**对着一个已封禁的账号继续发了十几个请求**。页面自己也在喊
「请勿频繁提交刷新请求」——每多发一个都可能延长限制。

两个独立的漏判原因：
1. `_PROMPT_CATEGORY` 的模式写成 `ip地址存在异常`，而平台文案是
   「您的 **IP** 存在异常行为」（`IP` 后有空格、且没有「地址」二字）
2. 封禁页不在 `_is_challenge_url()` 里，字节数（~44KB）又恰好落进
   JS 挑战页区间，于是按字节数兜底成了 `js_challenge`
"""
from __future__ import annotations

import pytest

from job_radar.collectors.boss import (
    _expire_to_iso, _page_prompt, _parse_block_page, _prompt_verdict, _ssr_blank_reason,
)

URL_32 = "https://www.zhipin.com/web/passport/zp/403.html?code=32"


def block_page(active_code: int, *, expire: str = "2026-10-21 00:01",
               codes=(31, 32)) -> str:
    """按实测结构造一个封禁页：多个 code-block，只有 active 那个带 show。"""
    blocks = []
    for code in codes:
        shown = " show" if code == active_code else ""
        if code == 31:
            body = '<p class="info-text">您的 IP 存在异常行为，请登录后使用。</p>'
        else:
            body = ('<p class="info-text"><span class="tips-32">'
                    '您的账户存在异常行为，已暂时被限制访问！</span></p>')
        blocks.append(
            f'<div id="code{code}" class="code-block{shown}" style="display: block;">'
            f'<div class="info-card">{body}'
            f'<p class="info-text">将于 <span class="expire-time highlight">{expire}</span>'
            f' 恢复正常</p>'
            f'<p class="info-hint">请勿频繁提交刷新请求</p></div></div>')
    return (
        '<html><body><h2 class="main-title">访问受限</h2>'
        '<p class="sub-title">抱歉，您暂时无法访问此页面</p>'
        + "".join(blocks)
        + "</body></html>")


def test_parses_active_block_not_first_block():
    """页里同时有 code31（IP）和 code32（账户），生效的是带 show 的 32。"""
    page = _parse_block_page(block_page(32))
    assert page is not None
    assert page["code"] == 32
    assert page["all_codes"] == [31, 32]
    assert page["category"] == "account_restriction"


def test_parses_active_ip_block():
    page = _parse_block_page(block_page(31))
    assert page is not None
    assert page["code"] == 31
    assert page["category"] == "network_restriction"


def test_extracts_expire_time():
    page = _parse_block_page(block_page(32, expire="2026-10-21 00:01"))
    assert page is not None
    assert page["expire_at"] == "2026-10-21 00:01"


def test_returns_none_on_normal_page():
    """普通岗位页没有 code-block，返回 None 让其他判据接手。"""
    assert _parse_block_page('<html><div class="job-sec-text">职责</div></html>') is None


def test_ssr_blank_reason_reports_account_restriction():
    """**核心回归**：真正的封禁必须判成 `account_restricted` + code=32。"""
    diag = _ssr_blank_reason(block_page(32), URL_32, 200)
    assert diag["kind"] == "account_restricted"
    assert diag["category"] == "account_restriction"
    assert diag["code"] == 32
    assert diag["evidence"] == "block:32"
    assert diag["expire_at"] == "2026-10-21 00:01"


def test_ssr_blank_reason_never_says_js_challenge_for_banned_page():
    """判成 js_challenge 会导致「等 2 秒自愈」然后继续加压 —— 绝不能出现。"""
    for code in (31, 32, 35):
        diag = _ssr_blank_reason(block_page(code, codes=(code,)), URL_32, 200)
        assert diag["kind"] != "js_challenge", f"code={code} 被判成 js_challenge"


@pytest.mark.parametrize("prompt,expected", [
    ("您的 IP 存在异常行为，请登录后使用", ("network_restriction", 35)),
    ("您的IP地址存在异常行为", ("network_restriction", 35)),
    # 纯文本判不出 code（verify.html 是 36 / 403.html 是 32），留给块结构补。
    ("您的账户存在异常行为，已暂时被限制访问！", ("account_restriction", None)),
    ("访问过于频繁，请稍后再试", ("rate_limited", None)),
])
def test_prompt_verdict_tolerates_spaces(prompt, expected):
    """平台文案里 `IP 存在异常` 和 `IP地址存在异常` 都出现过，空白必须容忍。"""
    assert _prompt_verdict(prompt) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-21 00:01", "2026-10-21T00:01:00+08:00"),
    ("2026-10-21 00:01:30", "2026-10-21T00:01:30+08:00"),
    ("看不懂", ""),
    ("", ""),
])
def test_expire_to_iso(raw, expected):
    assert _expire_to_iso(raw) == expected


def test_page_prompt_still_works_on_ban_page():
    """`_page_prompt` 是通用的兜底口径，封禁页上也该取到平台原话。"""
    prompt = _page_prompt(block_page(32))
    assert "异常" in prompt or "受限" in prompt
