"""体检波尔出口：出口 IP + BOSS 封禁状态 + 精确解封时间。一条 curl，不起浏览器。

判据来自 BOSS 自己的接口 `/wapi/zpsecureflow/forbidden-info`（2026-08-27 发现）：
    正常     → zpData 为 null（或不含 code=31）
    被限制   → zpData.code=31 + expireTime(ms) + ip + tip
               带 jumpVerify=true → 可人工点验证救回
               不带 jumpVerify   → 硬封，只能等到 expireTime

⚠️ 不要用这三种判据，都试过是错的：
  · **BOSS 首页返回码**：假绿灯。实测首页 200 而详情页全是「安全验证」。
  · **裸 curl 打 job_detail**：正常 IP 的 JS 挑战同样是 302 到 security.html，
    与「已被标记」无法区分。
  · **读 403 页面文本**：解封时间由 JS 从上面那个接口填，抓 HTML 拿不到，
    渲染前读会得到「将于 恢复正常」这种缺时间的碎片。

用法：
    uv run python scripts/exit_health.py            # 体检默认端口
    uv run python scripts/exit_health.py 1080 1082  # 只看指定端口
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

DEFAULT_PORTS = [1080, 1081, 1082]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INFO_URL = "https://www.zhipin.com/wapi/zpsecureflow/forbidden-info?"
IP_URL = "https://myip.ipip.net/"


def curl(port: int, url: str, timeout: int = 20) -> str | None:
    try:
        out = subprocess.run(
            ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
             "--max-time", str(timeout), "-H", f"User-Agent: {UA}",
             "-H", "X-Requested-With: XMLHttpRequest", url],
            capture_output=True, text=True, timeout=timeout + 6)
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def exit_ip(port: int) -> str:
    """出口 IP。第三方站点会偶发超时，所以只当展示用，不作去重依据。"""
    raw = curl(port, IP_URL)
    if not raw:
        return "(第三方 IP 站无响应)"
    return raw.replace("当前 IP：", "").strip()


def status(port: int) -> tuple[str, str]:
    """返回 (verdict, 说明)。verdict: ok / verify / blocked / unknown / dead"""
    raw = curl(port, INFO_URL)
    if raw is None:
        return "dead", "隧道不通"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown", f"非 JSON 响应: {raw[:80]}"
    zp = body.get("zpData")
    if not zp or zp.get("code") != 31:
        return "ok", "无限制记录"
    exp = zp.get("expireTime")
    when = ""
    if isinstance(exp, (int, float)):
        d = datetime.fromtimestamp(exp / 1000)
        left = (d - datetime.now()).total_seconds() / 3600
        when = f"解封 {d:%Y-%m-%d %H:%M}（还剩 {left:.1f} 小时）"
    ip = zp.get("ip") or "?"
    if zp.get("jumpVerify"):
        return "verify", f"IP {ip} 被标记，可人工点验证救回。{when}"
    return "blocked", f"IP {ip} 硬封，人工验证无效，只能等。{when}"


def main() -> None:
    ports = [int(a) for a in sys.argv[1:]] or DEFAULT_PORTS
    print(f"体检时间 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    # 去重依据用 forbidden-info 自己回的 zpData.ip，不用第三方 IP 站——
    # 那个站会偶发超时，一超时就把不同出口误判成同一个（踩过）。
    for p in ports:
        v, note = status(p)
        print(f":{p}  {exit_ip(p)}\n     {v} — {note}\n")


if __name__ == "__main__":
    main()
