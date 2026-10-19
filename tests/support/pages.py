"""回放服务要吐的几种页面。刷新链路的两套测试共用这一份。

页面结构照真实岗位页抄：状态在 `.job-banner` 里，正文在 `.job-sec-text` 里，
**并且额外放一张"相似职位"卡片**——它里面也有 `.job-status`。取值必须拿到 banner
里那个，拿错就会把在招岗位判成关闭。

长度是硬要求：正常岗位页实测 138–147KB，而 `MIN_JOB_HTML_LEN` 是 60KB。
短于这个数会被判成"短页面"，那是另一条分支，不是这些用例要测的。
"""

from __future__ import annotations

# 约 70,000 字符，保证整页跨过 60KB 的下界。
FILLER = "岗位职责与任职要求。" * 7_000


def job_page(job_status: str = "招聘中", *, h1: str = "AI Agent 开发工程师",
             active: str = "今日活跃") -> str:
    return (
        "<html><body>"
        '<div class="info-primary">'
        '  <div class="job-banner">'
        f'    <div class="job-status"><span>{job_status}</span></div>'
        f'    <div class="name"><h1 title="{h1}">{h1}</h1>'
        '      <span class="salary">20-40K</span></div>'
        "  </div>"
        f'  <div class="boss-active-time">{active}</div>'
        "</div>"
        f'<div class="job-sec-text">{FILLER}</div>'
        '<div class="similar-job">'
        '  <div class="job-status">职位已关闭</div>'
        '  <div class="similar-job-name">别的公司的岗位</div>'
        "</div>"
        "</body></html>"
    )


def gone_page() -> str:
    """已下架：约 11KB、404、含「页面不存在」。和挑战页都是短页，处置相反。"""
    return ("<html><body><div class='error'>您访问的页面不存在</div>"
            + "<p>去看看别的职位</p>" * 600 + "</body></html>")


def challenge_page() -> str:
    """JS 挑战页：约 41KB、含 `security-check`。"""
    return ("<html><body><script src='/security-check.js'></script>"
            + "<p>请稍候</p>" * 4_000 + "</body></html>")


def verify_page() -> str:
    """验证页：44.5KB、200、含「安全验证」，**不含** `security-check`。"""
    return ("<html><head><title>安全验证 - BOSS直聘</title></head><body>"
            "<p>您的账号可能存在异常访问行为</p>"
            + "<p>请完成验证</p>" * 4_000 + "</body></html>")
