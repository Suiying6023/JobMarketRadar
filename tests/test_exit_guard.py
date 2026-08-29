"""采集出口自检测试。

背景（2026-08-29 实测）：本机系统代理开着（Clash `127.0.0.1:7897`），
**Chrome 无 `--proxy-server` 时会回落到系统代理** —— 采集 Chrome 的 12 条连接
全部指向 7897。当时安全只因为 Clash 对 zhipin 走 DIRECT（两条路径出口 IP
逐字节相同），但那是分流规则的偶然结果：规则改一次或切全局模式，
账号 cookie 就会从机房节点发出去，而这**不会报错**，只表现为频繁触发验证。

所以守卫的核心要求是 **fail-closed**：任何"证明不了出口是家宽"的情形都要拒绝，
不能因为拿不到数据就放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar import exit_guard as G  # noqa: E402


def _fake_ips(monkeypatch, *, proxy, direct):
    """替掉 `_exit_ip`，按是否走代理返回不同 IP。"""
    monkeypatch.setattr(
        G, "_exit_ip",
        lambda via_proxy, timeout=20: proxy if via_proxy else direct)


def test_passes_when_proxy_is_transparent_for_zhipin(monkeypatch):
    """代理放行 zhipin（两条路径同一 IP）时应通过，并回那个 IP。

    这是当前本机的真实状态：连接经 7897，但出口仍是家宽 223.160.209.18。
    """
    _fake_ips(monkeypatch, proxy="223.160.209.18", direct="223.160.209.18")
    assert G.assert_home_exit(verbose=False) == "223.160.209.18"


def test_refuses_when_proxy_exit_differs_from_direct(monkeypatch):
    """出口不一致 = 账号 cookie 会从陌生 IP 发出，必须拒绝启动。

    这是这个模块存在的唯一理由：它是静默的，不拦就只会表现成"突然老是要验证"。
    """
    _fake_ips(monkeypatch, proxy="47.92.193.127", direct="223.160.209.18")
    with pytest.raises(G.ExitGuardError) as exc:
        G.assert_home_exit(verbose=False)
    # 报错要同时给出两个 IP，否则人得自己再查一遍才知道差在哪
    assert "47.92.193.127" in str(exc.value)
    assert "223.160.209.18" in str(exc.value)


def test_refuses_when_direct_exit_unknown(monkeypatch):
    """直连拿不到 IP 就无法证明出口是家宽 —— fail-closed，不能放行。

    这一条正是"用 zpData.ip 而不是第三方 IP 站"的原因：第三方站会偶发超时，
    若把"两边都没响应"当成"两边一致"，判据本身就成了误放行的通道。
    """
    _fake_ips(monkeypatch, proxy="223.160.209.18", direct=None)
    with pytest.raises(G.ExitGuardError):
        G.assert_home_exit(verbose=False)


def test_refuses_when_both_unknown(monkeypatch):
    """两边都拿不到（网络不通）同样拒绝：那会在浏览器里表现成一堆超时，
    与风控难以区分，不该开跑。"""
    _fake_ips(monkeypatch, proxy=None, direct=None)
    with pytest.raises(G.ExitGuardError):
        G.assert_home_exit(verbose=False)


def test_collection_scripts_check_the_exit_before_spending_quota():
    """两个采集链路脚本都必须接上守卫。

    钉死这条是因为漏接不会报错 —— 脚本照常跑完，只是失去了保护。
    `sustained_run.py` 不用单独接：它是通过子进程调这两个脚本的。
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for name in ("backfill_details.py", "expand_list.py"):
        src = (scripts / name).read_text(encoding="utf-8")
        assert "assert_home_exit()" in src, f"{name} 没有调用出口自检"
