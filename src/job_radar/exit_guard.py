"""采集链路的出口自检：确认账号 cookie 不会从陌生 IP 发出去。

**为什么需要它**：整条采集链路的风控模型建立在「家宽住宅 IP + 账号 cookie」上，
住宅 IP 的可信度是这条链路能跑到日线 1800 的前提。而实测发现（2026-08-29）
本机系统代理是开着的（Clash `127.0.0.1:7897`，`ProxyEnable=1`），
**Chrome 在没有 `--proxy-server` 时会回落到系统代理** ——
采集 Chrome 的 12 条连接全部指向 7897，零条直连。

当时是安全的：Clash 对 `zhipin.com` 走 DIRECT，经代理与绕代理的出口 IP
逐字节相同（`223.160.209.18`，家宽）。**但那是分流规则的偶然结果，不是保证。**
规则改一次、或切到全局模式，采集就会静默从某个机房节点出去，
而账号 cookie 还带着 —— 那是风控最敏感的组合（免登录走机房只是被判更狠，
带账号 cookie 走陌生 IP 是另一个量级）。

**它不会报错**，只表现为「突然开始频繁触发验证」，而那时人会先去查节流参数。
所以把这个隐含前提变成启动时的显式检查。

判据是拿 BOSS 自己的 `forbidden-info` 接口对比两条路径回的 `zpData.ip`：
- 相同 → 代理放行了 zhipin，等价于直连，可以跑
- 不同 → 采集会从代理出口发出去，**拒绝启动**
- 直连拿不到（比如真的只有代理能出网）→ 拒绝启动，因为无法证明出口是家宽

⚠️ 用 `zpData.ip` 而不是第三方 IP 站：后者会偶发超时，一超时就把
两条路径都判成「无响应」，看起来像"两边一致"从而放行 —— 判据本身要防误放。
"""
from __future__ import annotations

import json
import subprocess

INFO_URL = "https://www.zhipin.com/wapi/zpsecureflow/forbidden-info?"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class ExitGuardError(RuntimeError):
    """出口不是家宽，或无法证明是家宽。调用方应中止采集。"""


def _exit_ip(via_proxy: bool, timeout: int = 20) -> str | None:
    """问一次 BOSS 自己的接口拿出口 IP。`None` = 没拿到（超时/非 JSON）。

    `--noproxy '*'` 让 curl 忽略 `*_proxy` 环境变量，是"绕开代理"那一路；
    默认那一路会跟随环境变量与系统代理，代表浏览器实际走的路径。
    """
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-H", f"User-Agent: {UA}"]
    if not via_proxy:
        cmd += ["--noproxy", "*"]
    cmd.append(INFO_URL)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 6)
    except Exception:
        return None
    try:
        zp = (json.loads((out.stdout or "").strip()) or {}).get("zpData") or {}
    except json.JSONDecodeError:
        return None
    return zp.get("ip") or None


def assert_home_exit(verbose: bool = True) -> str:
    """确认采集流量的出口就是直连出口。返回那个 IP，不一致则抛 ExitGuardError。"""
    effective = _exit_ip(via_proxy=True)
    direct = _exit_ip(via_proxy=False)

    if effective is None:
        raise ExitGuardError(
            "拿不到采集路径的出口 IP（forbidden-info 无响应）。"
            "网络不通时不要开跑——那会在浏览器里表现成一堆超时，难与风控区分。")
    if direct is None:
        raise ExitGuardError(
            f"直连拿不到出口 IP，无法证明采集出口 {effective} 是家宽。"
            "若确实只有代理能出网，说明账号 cookie 会从代理出口发出去，"
            "而这条链路的风控前提是住宅 IP —— 先解决直连再跑。")
    if effective != direct:
        raise ExitGuardError(
            f"⚠️ 采集出口 {effective} ≠ 直连出口 {direct}。"
            "系统代理把 zhipin 流量转出去了，账号 cookie 会从陌生 IP 发出 —— "
            "这是风控最敏感的组合。把 zhipin.com 加进代理的 DIRECT 规则，"
            "或关掉系统代理后重跑。")
    if verbose:
        print(f"[出口自检] 采集出口 = 直连出口 = {effective}（家宽，可跑）")
    return effective
