"""体检跳板出口：出口 IP + BOSS 封禁状态 + 精确解封时间。一条 curl，不起浏览器。

判据与出口池都来自 `src/job_radar/exits.py`（单一真源）。那里也写了各 verdict
该怎么处置，以及这个接口的两个已知边界（空响应不是 ok、接口回 ok 不等于岗位页能读）。

⚠️ 不要用这三种判据，都试过是错的：
  · **BOSS 首页返回码**：假绿灯。实测首页 200 而详情页全是「安全验证」。
  · **裸 curl 打 job_detail**：正常 IP 的 JS 挑战同样是 302 到 security.html，
    与「已被标记」无法区分。
  · **读 403 页面文本**：解封时间由 JS 从状态接口填，抓 HTML 拿不到，
    渲染前读会得到「将于 恢复正常」这种缺时间的碎片。

用法：
    uv run python scripts/exit_health.py                 # 体检出口池全部
    uv run python scripts/exit_health.py --ports 1080    # 只看指定端口
    uv run python scripts/exit_health.py --direct        # 一并查本机直连
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.exits import (  # noqa: E402
    DIRECT_LABEL, UA, exit_pool, exit_verdict,
)

IP_URL = "https://myip.ipip.net/"


def exit_ip(port: int, timeout: int = 20) -> str:
    """出口 IP，**只当展示用**。

    去重依据要用 `forbidden-info` 自己回的 `zpData.ip`：这个第三方站会偶发超时，
    一超时两个不同出口都变成「无响应」，就被误判成同一个 IP（踩过）。
    """
    try:
        out = subprocess.run(
            ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
             "--max-time", str(timeout), "-H", f"User-Agent: {UA}", IP_URL],
            capture_output=True, text=True, timeout=timeout + 6)
        raw = (out.stdout or "").strip()
    except Exception:
        return "(第三方 IP 站无响应)"
    return raw.replace("当前 IP：", "").strip() or "(第三方 IP 站无响应)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ports", default="",
                    help="只体检这些端口（逗号分隔）。默认走出口池全部")
    ap.add_argument("--direct", action="store_true",
                    help="一并查本机直连出口（采集链路走的家宽）")
    ap.add_argument("--timeout", type=int, default=20, help="单次 curl 超时秒")
    args = ap.parse_args()

    if args.ports:
        targets: list[tuple[str, int | None]] = [
            (f":{p.strip()}", int(p)) for p in args.ports.split(",") if p.strip()]
    else:
        targets = [(t, p) for t, p in exit_pool()]
    if args.direct:
        targets.append((DIRECT_LABEL, None))

    print(f"体检时间 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    bad = 0
    for label, port in targets:
        verdict, note = exit_verdict(port, timeout=args.timeout)
        where = f"{label}" + (f" :{port}  {exit_ip(port, args.timeout)}"
                              if port else "  (本机直连)")
        print(f"{where}\n     {verdict} — {note}\n")
        if verdict != "ok":
            bad += 1
    # 退出码给调用方用：0 = 全部可用，1 = 有出口不可用。
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
