"""出口状态判读：`jumpVerify` 为真也可能过不去。

实测（2026-09-01，家宽 IP）：`forbidden-info` 回

    {"code":31, "jumpVerify":true, "ip":"...",
     "tip":"您的 IP 存在异常行为，请登录后使用。",
     "jumpUrl":"/web/passport/zp/verify.html"}

验证页能打开（43677 字节，geetest 脚本齐全、静态资源也可达），
但**验证组件不加载** —— 平台给这个 IP 的出路写在 `tip` 里：**请登录后使用**。

只看 `jumpVerify` 会把它报成「可人工过验证」，让人对着一个不会出现的组件干等，
而这条链路必须免登录，所以真正的出路只有等。

判据：**一个"可以补救"的标志位，还要看补救通道是否真的开着。**
`jumpVerify` 说的是"这个限制类型允许验证"，不是"现在能验证"。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from job_radar.exits import exit_verdict

ROOT = Path(__file__).resolve().parent.parent


class _Out:
    def __init__(self, body: str):
        self.stdout = body
        self.stderr = ""


def _resp(**zp) -> str:
    return json.dumps({"code": 0, "message": "Success", "zpData": zp})


@pytest.mark.parametrize("tip,expected", [
    ("您的 IP 存在异常行为，请登录后使用。", "login_only"),
    ("请登录后使用", "login_only"),
    ("您的账户存在异常行为，已暂时被限制访问！", "verify"),
    ("", "verify"),
])
def test_jump_verify_with_login_tip_is_not_savable(tip, expected):
    body = _resp(code=31, ip="1.2.3.4", jumpVerify=True,
                 expireTime=1788280238000, tip=tip)
    with patch("subprocess.run", return_value=_Out(body)):
        verdict, note = exit_verdict(1080)
    assert verdict == expected, f"tip={tip!r} 应判 {expected}，得到 {verdict}（{note}）"


def test_login_only_note_says_do_not_pop_a_window():
    body = _resp(code=31, ip="1.2.3.4", jumpVerify=True,
                 tip="您的 IP 存在异常行为，请登录后使用。")
    with patch("subprocess.run", return_value=_Out(body)):
        _, note = exit_verdict(1080)
    assert "组件不加载" in note or "点也点不了" in note, \
        "说明里要讲清「点也点不了」，否则下一次还会去弹窗"


def test_hard_block_without_jump_verify():
    body = _resp(code=31, ip="1.2.3.4", expireTime=1788280238000,
                 tip="您的 IP 存在异常行为")
    with patch("subprocess.run", return_value=_Out(body)):
        verdict, _ = exit_verdict(1080)
    assert verdict == "blocked"


def test_empty_response_is_dead_not_ok():
    """空响应是隧道不通，**不是 ok** —— 旧代码把它 json 成 {} 后报了假绿灯。"""
    with patch("subprocess.run", return_value=_Out("")):
        verdict, _ = exit_verdict(1080)
    assert verdict == "dead"


def test_ok_requires_an_ip_in_the_response():
    """`code != 31` 但没有 ip 也不能当 ok（那是异常响应）。"""
    with patch("subprocess.run", return_value=_Out(json.dumps({"zpData": {}}))):
        verdict, _ = exit_verdict(1080)
    assert verdict == "unknown"
    with patch("subprocess.run", return_value=_Out(_resp(ip="1.2.3.4"))):
        verdict, _ = exit_verdict(1080)
    assert verdict == "ok"


def test_callers_handle_login_only():
    """所有按 verdict 分支的调用方都要认 login_only，否则它会落进 ok 分支。"""
    for rel in ("scripts/refresh_status_loop.py", "scripts/watch_chains.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "login_only" in src, f"{rel} 没处理 login_only"


# ---- 限制码可能在顶层，且 zpData 为空 ----

@pytest.mark.parametrize("code,who", [(35, "IP"), (36, "账号")])
def test_top_level_restriction_code_is_login_only(code, who):
    """实测家宽：`{"code":35,"message":"您的IP地址存在异常行为.","zpData":{}}`。

    验证页框架会渲染（标题变「安全验证」、正文写「请完成以下验证」），
    但服务端不下发挑战参数 —— **组件永远不出现，人卡在验证页上点不了**。
    只看 `zpData.code` 会因为拿不到 ip 而报 `unknown`，
    把一个明确的限制说成"响应异常"。
    """
    body = json.dumps({"code": code, "message": f"您的{who}地址存在异常行为.",
                       "zpData": {}})
    with patch("subprocess.run", return_value=_Out(body)):
        verdict, note = exit_verdict(None)
    assert verdict == "login_only", f"顶层 code={code} 应判 login_only，得到 {verdict}"
    assert "点不了" in note or "不下发" in note


def test_top_level_code_zero_with_zpdata_still_uses_zpdata():
    """正常响应的顶层 `code` 是 0，限制信息在 `zpData` 里 —— 别把这条也吃掉。

    这是 vm 的真实响应形态（2026-09-01）：tip 同样说「请登录后使用」，
    但**没有 `jumpVerify`** → 硬封。三种状态的 tip 可以完全一样，
    区分只在 `jumpVerify` 有无与限制码在哪一层。
    """
    hard = json.dumps({"code": 0, "message": "Success",
                       "zpData": {"code": 31, "ip": "1.2.3.4",
                                  "expireTime": 1788281044000,
                                  "tip": "您的 IP 存在异常行为，请登录后使用。"}})
    with patch("subprocess.run", return_value=_Out(hard)):
        verdict, _ = exit_verdict(1080)
    assert verdict == "blocked", "无 jumpVerify 就是硬封，哪怕 tip 提到登录"

    healthy = json.dumps({"code": 0, "message": "Success",
                          "zpData": {"ip": "1.2.3.4"}})
    with patch("subprocess.run", return_value=_Out(healthy)):
        verdict, _ = exit_verdict(1080)
    assert verdict == "ok"
