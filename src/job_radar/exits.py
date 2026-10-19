"""跳板出口池 —— 状态刷新链路的 SOCKS 出口定义。**这是唯一真源。**

之前 `refresh_status_loop.py` / `watch_chains.py` / `exit_health.py`
/ `proxies.bohrium.txt` 各写一份端口，已经漂出过两次：
一次是 container 写 1081 而脚本写 1082（症状是脚本判 container "dead" 并
**静默降级到单出口**，不报错），一次是体检脚本留着已不存在的 1081
（每轮体检必报一条假 dead）。

## 现状

两台波尔各一条 `ssh -D` 隧道，都是阿里云机房 IP：

| 标识 | 端口 | 说明 |
|---|---:|---|
| `vm` | 1080 | `bohrium-vm` |
| `container` | 1082 | `bohrium-container` |

⚠️ **隧道不持久，每次开机都要重起**：

    ssh -f -N -D 1080 -o ServerAliveInterval=30 bohrium-vm
    ssh -f -N -D 1082 -o ServerAliveInterval=30 bohrium-container

⚠️ **端口 LISTENING 不等于流量通**。SSH 转发死掉后端口仍在监听，但 curl 会超时，
表现是整批 100% 撞挑战页 —— 看起来像被风控。判据是真发一次请求，不是看端口。

⚠️ **波尔只做 SOCKS 出口转发，机器上不装任何东西**（不需要 Python、不需要浏览器）。

⚠️ **这条链路必须免登录**：带账号 cookie 走跳板 IP 等于用陌生 IP 操作主账号，
风控风险远大于收益。相关脚本都有守卫会拒绝这种组合。
"""
from __future__ import annotations

# 采集链路走本机家宽直连，没有端口。巡检需要把它和跳板出口并列展示时用这个。
DIRECT_LABEL = "家宽"

# BOSS 自己的出口状态接口：一条 curl 就能拿到「这个 IP 什么状态、什么时候解封」。
# 判据见 `exit_verdict()`。
FORBIDDEN_INFO_URL = "https://www.zhipin.com/wapi/zpsecureflow/forbidden-info?"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def exit_verdict(port: int | None, timeout: int = 20) -> tuple[str, str]:
    """查一个出口的封禁状态。`port=None` 走直连（采集侧家宽）。

    返回 `(verdict, 人类可读说明)`，verdict 取值：

    | verdict | 含义 | 该怎么办 |
    |---|---|---|
    | `ok` | 无限制记录 | 可用 |
    | `verify` | 带 `jumpVerify`，且 tip 没让你登录 | **人工点掉验证即当场解除** |
    | `login_only` | 带 `jumpVerify` 但 tip 说「请登录后使用」，**或**顶层 `code` 是 35/36 且 `zpData` 为空 | **验证组件不会加载**，别弹窗给人点；免登录链路只能等 |
    | `blocked` | 硬封，无 `jumpVerify` | 人工验证无效，只能等 `expireTime` |
    | `dead` | 隧道不通 | 重起 ssh，**这不是风控** |
    | `unknown` | 响应非 JSON 或缺字段 | 别当 ok |

    ⚠️ **空响应是 `dead` 不是 `ok`**。踩过：隧道断了（端口仍 LISTENING 但流量不通），
    旧实现把空响应 json 成 `{}` → `code != 31` → 报 "ok"。那是假绿灯 ——
    真正的信息在「ip 取不到」里，却被 ok 盖住了。所以判据必须先确认
    **拿到了可解析的响应且里面有 ip**，再看 code。

    ⚠️ **这个接口有滞后**。实测两个出口整整一小时把岗位页 302 到 `verify.html`，
    而它全程回 `code:0` —— 所以它回 ok 不等于「岗位页能读」。
    要判后者必须真读一条 job_detail（见 `refresh_status_loop.probe_detail`）。

    ⚠️ **`expireTime` 是初次限制时定的固定时刻，不会因为再访问而推后**。
    带 `jumpVerify` 时不要报解封倒计时，要报「可人工过验证」。
    """
    import json
    import subprocess
    from datetime import datetime

    cmd = ["curl", "-sS", "--max-time", str(timeout),
           "-H", f"User-Agent: {UA}",
           "-H", "X-Requested-With: XMLHttpRequest"]
    if port:
        cmd[1:1] = ["--socks5-hostname", f"127.0.0.1:{port}"]
    cmd.append(FORBIDDEN_INFO_URL)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 6)
        raw = (out.stdout or "").strip()
    except Exception as exc:
        return "dead", f"隧道不通（{type(exc).__name__}）"
    if not raw:
        return "dead", "隧道无响应（端口可能还在 LISTENING 但转发已失效）"
    try:
        body = json.loads(raw) or {}
    except json.JSONDecodeError:
        return "unknown", f"非 JSON 响应: {raw[:60]}"
    zp = body.get("zpData") or {}

    # ⚠️ **限制码可能在顶层而不在 `zpData` 里**，且 `zpData` 会是空的。
    # 实测家宽（2026-09-01）：`{"code":35,"message":"您的IP地址存在异常行为.","zpData":{}}`
    # —— 验证页框架会渲染出来（标题变「安全验证」、正文写「请完成以下验证」），
    # 但服务端不下发挑战参数，所以**组件永远不出现，人卡在验证页上点不了**。
    # 只看 `zpData.code` 会因为拿不到 ip 而报 `unknown`，把一个明确的限制
    # 说成"响应异常"。已知的顶层限制码：35（IP 异常）、36（账号异常）。
    top = body.get("code")
    if not zp and top in (35, 36):
        who = "账号" if top == 36 else "IP"
        return "login_only", (
            f"{who}被限制（顶层 code={top}: {body.get('message', '')}）"
            f"—— 验证页会渲染但不下发挑战，点不了；无解封时间，只能等")

    if zp.get("code") != 31:
        ip = zp.get("ip")
        if not ip:
            return "unknown", "响应里没有 ip（异常，别当 ok）"
        return "ok", f"无限制记录 ip={ip}"

    ip = zp.get("ip") or "?"
    exp = zp.get("expireTime")
    when = ""
    if isinstance(exp, (int, float)):
        when = f" 解封 {datetime.fromtimestamp(exp / 1000):%m-%d %H:%M}"
    tip = zp.get("tip") or ""

    # ⚠️ **`jumpVerify` 为真也可能过不去** —— 还要看 `tip` 给的是哪条出路。
    # 实测（2026-09-01，家宽）：`jumpVerify: true` 但 tip 是
    # 「您的 IP 存在异常行为，**请登录后使用**」，验证页能打开、geetest 脚本也在，
    # 但**验证组件不加载** —— 平台给这个 IP 的出路是登录，不是过验证。
    # 只看 `jumpVerify` 会报成「可人工过」，让人对着一个不会出现的组件干等。
    # 判据加一层：tip 里出现「登录」就说明验证这条路是空的。
    if zp.get("jumpVerify") and "登录" not in tip:
        return "verify", f"IP {ip} 被标记，可人工过验证当场解除。{when}"
    if zp.get("jumpVerify"):
        return "login_only", (
            f"IP {ip} 被标记且平台只给「登录」这条路（tip: {tip}）——"
            f"验证页会打开但组件不加载，点也点不了。{when}")
    return "blocked", f"IP {ip} 硬封，人工验证无效，只能等。{when}"
