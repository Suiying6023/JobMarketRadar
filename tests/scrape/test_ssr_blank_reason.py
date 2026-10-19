"""详情页空正文的归因（`_ssr_blank_reason`）。

这段逻辑的价值全在**别把四种不同的事混成一种**：
  · 平台要人工验证（风控信号，必须停）
  · 岗位真的下线了（正常，不算问题）
  · 未登录只能看预览（账号问题，必须停）
  · 我们的选择器失效了（我们自己的 bug）

混起来的代价是双向的：真下线被当成风控而白停一轮，风控被当成下线而继续加压，
选择器失效则长期无声地丢正文。
"""
from __future__ import annotations

import pytest

from job_radar.collectors.boss import _is_challenge_url, _ssr_blank_reason

URL = "https://www.zhipin.com/job_detail/abc123.html"


@pytest.mark.parametrize("url", [
    "https://www.zhipin.com/web/passport/zp/security.html?seed=x",
    "https://www.zhipin.com/web/passport/zp/verify.html?code=36",
    "https://www.zhipin.com/job_detail/x.html?safe-validate=1",
])
def test_challenge_urls_are_recognised(url):
    """`_collect_ssr_detail` 靠它决定「要不要等站点自己跳走再读」。"""
    assert _is_challenge_url(url) is True


@pytest.mark.parametrize("url", [
    "https://www.zhipin.com/job_detail/x.html",
    "https://www.zhipin.com/job_detail/x.html?_security_check=1_123",   # 已自愈的地址
    "https://www.zhipin.com/web/geek/jobs?query=Python",
])
def test_normal_urls_are_not_challenges(url):
    assert _is_challenge_url(url) is False


def reason(html: str, url: str = URL, status: int = 200) -> str:
    return _ssr_blank_reason(html, url, status)["kind"]


def test_verification_wins_by_url():
    """地址栏是硬证据，优先级最高。"""
    assert reason("<html>x</html>",
                  "https://www.zhipin.com/web/passport/zp/verify.html?code=36") == "verification"
    assert reason("<html>x</html>",
                  "https://www.zhipin.com/job_detail/abc.html?safe-validate=1") == "verification"


def test_security_html_without_captcha_is_js_challenge():
    """`/web/passport/zp/security.html` 是风控阶梯第二级：JS 挑战，不是滑块。

    判据沿用 `service_jobs.page_session_check`：有验证控件才算要人。
    混为一谈会让我们要么白停一轮，要么把风控当成自己的 bug。
    """
    page = "<html>" + "x" * 42000 + "</html>"
    url = "https://www.zhipin.com/web/passport/zp/security.html?seed=abc"
    assert reason(page, url) == "js_challenge"


def test_security_html_with_slider_is_verification():
    page = "<html>请完成验证 拖动滑块</html>"
    url = "https://www.zhipin.com/web/passport/zp/security.html?seed=abc"
    assert reason(page, url) == "verification"


def test_challenge_served_on_job_detail_url_is_detected_by_size():
    """挑战页有时直接回在 `job_detail` 的 URL 上，地址栏看不出来。

    这时只能靠字节数：历史标定 JS 挑战页约 41KB，实测的 security.html 是 42,825。
    漏掉这一条会把真实的风控信号记成 `selector_miss`（我们自己的 bug）。
    """
    assert reason("<html>" + "x" * 42825 + "</html>") == "js_challenge"


def test_closed_job_is_recognised_and_not_retried_as_risk():
    assert reason("<html>该职位已下线</html>" + "x" * 200) == "closed"
    assert reason("<html>页面不存在</html>" + "x" * 11000) == "closed"


def test_login_wall_is_its_own_kind():
    assert reason("<html>登录后查看</html>" + "x" * 200) == "login_wall"


def test_selector_miss_vs_empty_page_vs_blank_jd():
    """三种"没有正文"要分开：结构变了 / 压根不是详情页 / 岗位没写 JD。"""
    assert reason("<html>" + "x" * 150000 + "</html>") == "selector_miss"
    assert reason("<html>" + "x" * 5000 + "</html>") == "selector_miss"
    assert reason("<html></html>") == "empty_page"
    assert reason('<div class="job-sec-text"></div>' + "x" * 500) == "blank_jd"


def test_size_threshold_uses_bytes_not_characters():
    """中文页面的 `len(str)` 只有字节数的三分之一。

    标定阈值（41KB 挑战页）是**响应体字节数**。用字符数比阈值会把 41KB 的挑战页
    算成 14K 字符，落进 `selector_miss` —— 把一个真实的风控信号记成我们自己的 bug。
    """
    chinese = "<html>" + "岗" * 14000 + "</html>"      # 14000 汉字 = 42000 字节
    diag = _ssr_blank_reason(chinese, URL, 200)
    assert diag["length"] == len(chinese.encode("utf-8"))
    assert diag["length"] > 40000, "应按字节计"
    assert len(chinese) < 15000, "字符数确实远小于字节数（否则这个用例没有意义）"
    assert diag["kind"] == "js_challenge"


@pytest.mark.parametrize("kind", ["verification", "js_challenge", "login_wall", "closed",
                                  "selector_miss", "empty_page", "blank_jd"])
def test_every_reason_carries_evidence(kind):
    """异常消息里必须带证据（状态码/字节数/URL/命中的词），否则等于没归因。"""
    samples = {
        "verification": ("<html>x</html>", "https://www.zhipin.com/web/passport/zp/verify.html"),
        "js_challenge": ("<html>" + "x" * 42000 + "</html>", URL),
        "login_wall": ("<html>请先登录</html>", URL),
        "closed": ("<html>职位已关闭</html>", URL),
        "selector_miss": ("<html>" + "x" * 9000 + "</html>", URL),
        "empty_page": ("<html></html>", URL),
        "blank_jd": ('<div class="job-sec-text"></div>', URL),
    }
    html, url = samples[kind]
    diag = _ssr_blank_reason(html, url, 200)
    assert diag["kind"] == kind
    assert diag["evidence"], "归因必须带证据"
    assert diag["length"] == len(html.encode("utf-8", "ignore"))
    assert diag["url"] == url
