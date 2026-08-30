"""刷新链路 fetch 判定的回归测试。

钉死两件在实测中真的踩过的事：
① `.job-status` **在招时也有文本**（「招聘中」/「最新」），所以判据必须看值。
   我最初写 `bool(text)`，24 条样本全判「关闭」；而那批样本恰好一半真关闭，
   单看那一半是 10/10 满分 —— 是平台 SEO 在招页那 8 条对照才暴露的。
② 未知文案要返回 None（记未判定），**不能猜**：误判关闭会单向降级一个在招岗位，
   误判在招只是下一轮再刷一次。两个方向的代价不对称。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.refresh_fetch import (  # noqa: E402
    FETCH_WINDOW, MIN_JOB_HTML_LEN, REQ_PER_JOB_AMORTIZED,
    is_closed_status, same_job,
)


def test_open_statuses_are_not_closed():
    """实测在招岗位的 `.job-status` 取值：SEO 在招页 8 条里 7 条「招聘中」、1 条「最新」。"""
    for v in ("招聘中", "最新"):
        assert is_closed_status(v) is False, f"{v} 是在招文案"


def test_closed_status_is_detected():
    assert is_closed_status("职位已关闭") is True


def test_nonempty_text_alone_must_not_mean_closed():
    """这条钉的就是那个 bug：有文本 != 关闭。

    `bool(text)` 写法下「招聘中」会被判成关闭，而在招岗位**全部**有这个文本，
    所以整库都会被误降级 —— 且降级不可逆。
    """
    assert is_closed_status("招聘中") is not True


def test_unknown_wording_returns_none_instead_of_guessing():
    """平台改文案时要记未判定，而不是猜。

    猜「关闭」→ 单向降级在招岗位（不可逆）；猜「在招」→ 下一轮再刷（便宜）。
    代价不对称，所以既不猜也不默认，交给调用方按未判定处理。
    """
    assert is_closed_status("招聘暂缓中") is None
    assert is_closed_status("") is None
    assert is_closed_status(None) is None


def test_same_job_guards_against_redirect_mixups():
    """身份校验：被重定向时判定会张冠李戴，而降级是单向的。"""
    assert same_job("智能体开发工程师", "智能体开发工程师")
    # 前后缀差异要能容忍（标题常带全角括号与方向后缀）
    assert same_job("Coding Agent评测工程师—(Java或py",
                    "Coding Agent评测工程师—(Java或python方向)")
    assert not same_job("前端开发工程师", "智能体开发工程师")
    assert not same_job("", "智能体开发工程师")
    assert not same_job(None, "智能体开发工程师")


def test_challenge_page_length_threshold_separates_the_two_shapes():
    """挑战页实测 41384 字节，正常岗位页 138736-147216 字节，阈值取 60000。"""
    assert 41_384 < MIN_JOB_HTML_LEN < 138_736


def test_amortized_cost_reflects_measured_window_and_recovery():
    """成本模型要贴实测：窗口约 5.5 条、恢复 3 次请求 → 摊薄 1.58 次/条。

    这个数替代了原先写死的 `REQ_PER_JOB = 14`（那是从 verify 页数出来的，
    而实测整页加载是 102 次 —— 旧常量既高估了 fetch 也低估了整页）。
    """
    assert 1.0 < REQ_PER_JOB_AMORTIZED < 2.0
    assert FETCH_WINDOW <= 6, "窗口实测 [6,5,6,5]，取值不能超过下界"
