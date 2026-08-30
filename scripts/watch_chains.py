"""长跑巡检：一次输出两条链路的健康快照，供每 15 分钟调用。

**设计意图**：长跑时人不在盯屏幕，所以巡检要能一眼回答三个问题
  ① 还在跑吗（进程是否存活）
  ② 有没有撞验证（这是唯一必须立刻处理的事）
  ③ 额度还剩多少 / 产出多少

⚠️ **判据用「进程存活 + RISK-STOP 标记」，不用日志的最后一行**：
长跑日志末尾常常是长停顿提示，看起来像"卡住了"，其实正常。
而 `[RISK-STOP]` 是两个脚本在**主动停止**时才打的唯一标记
（`code=36/32` 命中风控、或所有出口被标记），grep 它不会误报。

⚠️ 出口/账号状态一律现场查 `forbidden-info`，不读日志里的历史结论 ——
那是快照，可能已经过期。
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INFO = "https://www.zhipin.com/wapi/zpsecureflow/forbidden-info?"

# 日志按 glob 取**最新一份**，不写死日期。
# 写死日期的代价实测过：跨零点续跑后，巡检还在读前一天的日志，
# 于是持续汇报昨天那条已经处理完的 BUDGET-STOP，而看不到新批次的进度。
# 「写死的标识符是定时炸弹」—— 它不报错，只会一直给出过期答案。
CHAINS = {
    "refresh": ("refresh_status_loop", "logs/refresh-slow-*.log"),
    "backfill": ("backfill_details", "logs/backfill-slow-*.log"),
}


def latest_log(pattern: str) -> Path | None:
    """取匹配 glob 的最新一份日志（按修改时间）。"""
    files = sorted(ROOT.glob(pattern), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def alive(needle: str) -> int:
    """数匹配 CommandLine 的 python 进程。uv 启动器会多一个，所以 >=1 即算活。"""
    ps = (f"(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{needle}*' }}).Count")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def exit_state(port: int | None) -> str:
    """现场查封禁状态。port=None 走直连（采集侧家宽）。"""
    cmd = ["curl", "-sS", "--max-time", "20", "-H", f"User-Agent: {UA}",
           "-H", "X-Requested-With: XMLHttpRequest"]
    if port:
        cmd += ["--socks5-hostname", f"127.0.0.1:{port}"]
    cmd.append(INFO)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        raw = (out.stdout or "").strip()
    except Exception as exc:
        return f"⚠️ 查不到（{type(exc).__name__}）"
    # ⚠️ 空响应 = 隧道不通（curl 超时），**不是 ok**。
    # 实测踩到：container 隧道断了（端口仍 LISTENING 但流量不通），
    # 而旧代码把空响应 json 成 {} → code != 31 → 报 "ok ip=?"。
    # 那是假绿灯：真正的信息在「ip 取不到」里，却被 ok 盖住了。
    # 判据必须先确认**拿到了可解析的响应**，再看 code。
    if not raw:
        return "⚠️ 隧道不通（无响应，端口可能还在 LISTENING 但转发已失效）"
    try:
        zp = (json.loads(raw) or {}).get("zpData") or {}
    except json.JSONDecodeError:
        return f"⚠️ 非 JSON 响应: {raw[:50]}"
    if zp.get("code") != 31:
        ip = zp.get("ip")
        if not ip:
            return "⚠️ 响应里没有 ip（异常，别当 ok）"
        return f"ok  ip={ip}"
    kind = "verify(可人工过)" if zp.get("jumpVerify") else "blocked(硬封)"
    exp = zp.get("expireTime")
    when = ""
    if isinstance(exp, (int, float)):
        d = datetime.fromtimestamp(exp / 1000)
        when = f" 解封 {d:%m-%d %H:%M}"
    return f"⛔ {kind} ip={zp.get('ip', '?')}{when}"


def main() -> None:
    print(f"===== 巡检 {datetime.now():%H:%M:%S} =====")

    risk = False
    for name, (needle, log) in CHAINS.items():
        n = alive(needle)
        p = latest_log(log)
        tail, hits, budget = "", [], []
        if p and p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            # 只认 RISK-STOP。**BUDGET-STOP 不算异常** —— 它是日线到顶的正常
            # 预算行为，明日自然恢复。两者曾共用一个标记，导致 900/900 正常停机
            # 被报成"需要人工介入"（2026-08-30 实测踩到）。
            hits = [l for l in lines if "[RISK-STOP]" in l]
            budget = [l for l in lines if "[BUDGET-STOP]" in l]
            # 取最后一条**有进度语义**的行，跳过节奏/停顿提示
            for l in reversed(lines):
                if re.search(r"进度|完成|本轮|待检测|待补", l):
                    tail = l.strip()[:70]
                    break
        state = "运行中" if n >= 1 else "已结束"
        print(f"  {name:<9} {state:<7} {tail}"
              + (f"   [{p.name}]" if p else "   [无日志]"))
        if budget and not hits:
            print(f"    💰 额度到顶正常停止（无需人工）：{budget[-1].strip()[:80]}")
        if hits:
            risk = True
            print(f"    ⛔ RISK-STOP: {hits[-1].strip()[:100]}")

    print("  出口:")
    tunnel_down = []
    for label, port in (("vm", 1080), ("container", 1082), ("家宽", None)):
        s = exit_state(port)
        print(f"    {label:<10} {s}")
        if s.startswith("⛔"):
            risk = True          # 被风控标记 → 要人工过验证
        elif s.startswith("⚠️") and port:
            tunnel_down.append((label, port))   # 隧道断 → 重起即可，不用人
    if tunnel_down:
        print("  隧道不通（**不是风控**，重起即可，不需要人工过验证）:")
        for label, port in tunnel_down:
            print(f"    ssh -f -N -D {port} -o ServerAliveInterval=30 "
                  f"bohrium-{'vm' if label == 'vm' else 'container'}")

    con = sqlite3.connect(DB, timeout=30.0)
    day = datetime.now().strftime("%Y-%m-%d")
    r = con.execute("SELECT count(*) FROM request_log WHERE day=? "
                    "AND kind='detail_page'", (day,)).fetchone()[0]
    c = con.execute("SELECT count(*) FROM request_log WHERE day=? AND kind IN "
                    "('detail','list','security_check')", (day,)).fetchone()[0]
    jd = con.execute("SELECT count(*) FROM jobs WHERE job_description!=''").fetchone()[0]
    left_d = con.execute("SELECT count(*) FROM jobs WHERE job_description='' "
                         "AND retention_status='active'").fetchone()[0]
    left_r = con.execute(
        "SELECT count(*) FROM jobs WHERE retention_status='active' "
        "AND job_invalid=0 AND job_status_desc!='职位已关闭' "
        "AND job_description!=''").fetchone()[0]
    con.close()
    print(f"  额度: 刷新 {r}/1000   采集 {c}/900")
    print(f"  库存: 有JD {jd}  待补详情 {left_d}  待刷 {left_r}")
    print("  " + ("⛔ 需要人工介入" if risk else "✅ 无验证/风控信号"))
    sys.exit(2 if risk else 0)


if __name__ == "__main__":
    main()
