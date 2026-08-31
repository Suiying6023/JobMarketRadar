"""长跑巡检：一次输出两条链路的健康快照。

**设计意图**：长跑时人不在盯屏幕，所以巡检要能一眼回答三个问题
  ① 还在跑吗（进程是否存活）
  ② 有没有撞验证（这是唯一必须立刻处理的事）
  ③ 额度还剩多少 / 产出多少

用法：
    uv run python scripts/watch_chains.py                      # 默认档
    uv run python scripts/watch_chains.py --refresh-cap 1500   # 换日线
    uv run python scripts/watch_chains.py --verify-give-up 30  # 换放弃阈值

退出码：0 = 无需人工，2 = 需要人工介入（供调度器判断）。

## 判据（都是踩出来的，改之前先读）

- **用「进程存活 + RISK-STOP 标记」，不看日志最后一行**：长跑日志末尾常常是长停顿
  提示，看着像卡住其实正常。`[RISK-STOP]` 只在脚本**主动停止**时打，grep 它不误报。
- **`[BUDGET-STOP]` 不算异常**：那是日线到顶的正常预算行为，明日自然恢复。
  两者曾共用一个标记，导致 900/900 正常停机被报成"需要人工介入"。
- **出口/账号状态一律现场查，不读日志里的历史结论** —— 那是快照，可能已过期。
- **战果判据看账本，不看探测**：出口"看起来干净"不代表链路能干活。
  冷 curl 不带 stoken，**探不出"有会话之后会被弹到 verify.html"**，
  而账本里最近若干次取值全 challenged 就是在等人过验证。
- **链路没在跑时别报它的挑战率**：那是停机前的历史数据，无法行动。
- **某个备用出口坏了不该半夜叫人**：只要还有出口能用、链路在产出就不是急事。
  判据是「所有出口都坏 → ⛔，还剩好的 → ⚠️」。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.exits import DIRECT_LABEL, exit_pool, exit_verdict  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"

# 日志按 glob 取**最新一份**，不写死日期。写死的代价实测过：跨零点续跑后巡检还在读
# 前一天的日志，于是持续汇报昨天那条已处理完的停机，而看不到新批次的进度。
# 「写死的标识符是定时炸弹」—— 它不报错，只会一直给出过期答案。
CHAINS = {
    "refresh": ("refresh_status_loop", "logs/refresh-slow-*.log"),
    "backfill": ("backfill_details", "logs/backfill-slow-*.log"),
}

# 无人值守调度器。它在两段之间会有几十秒到几分钟的空档，此时两条链路都显示
# 「已结束」——⚠️ **那不是可以手工续跑的信号**：手工起一批会和调度器下一段撞成
# 两个同名进程，共用出口互相把 stoken 打掉。所以巡检先报「调度器在跑」。
SUPERVISOR = "overnight_supervisor"
STOP_FLAG = ROOT / "logs" / "SUPERVISOR-STOPPED.txt"


def latest_log(pattern: str) -> Path | None:
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


def verify_age_minutes() -> float | None:
    """最老的人工验证窗口已经挂了多少分钟。没有窗口返回 None。

    取**最老**那个而不是最新：判据是"这轮验证等了多久没人管"，
    而重弹时两个出口的窗口是先后起的，取最新会让计时反复归零。
    """
    ps = ("(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*manual_verify_proxy*' } | "
          "Sort-Object CreationDate | Select-Object -First 1).CreationDate | "
          "ForEach-Object { [int]((Get-Date) - $_).TotalMinutes }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        s = (out.stdout or "").strip()
        return float(s) if s else None
    except Exception:
        return None


def recent_challenge_rate(con: sqlite3.Connection, n: int) -> tuple[int, int]:
    """最近 n 次**取值**里 (ok, challenged) 各多少。链路能不能干活的唯一可靠判据。"""
    row = con.execute(
        "SELECT COALESCE(SUM(outcome='ok'),0), COALESCE(SUM(outcome='challenged'),0) "
        "FROM (SELECT outcome FROM request_log WHERE kind='detail_page' "
        "      AND outcome IN ('ok','challenged') ORDER BY id DESC LIMIT ?)", (n,)
    ).fetchone()
    return int(row[0]), int(row[1])


def verify_gate(port: int, job_url: str, timeout: int = 25) -> bool:
    """岗位页是否**不需要**人工验证。True=可用，False=撞上 verify.html。

    为什么要单独探这个：状态接口有滞后。实测两个出口整整一小时把岗位页 302 到
    `verify.html`，而同期 `forbidden-info` 一直回 `code:0`，那一小时的请求全部作废。

    `verify.html` 是 geetest 人工层，与可自解的 `security-check.html` **不是一回事**：
    后者纯计算、脚本自己能过；前者必须人点，静置不会恢复。所以只看 Location。

    用 `-o /dev/null -D -` 只取响应头：一次请求、不下载正文，成本最低。
    """
    from job_radar.exits import UA
    cmd = ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
           "--max-time", str(timeout), "-H", f"User-Agent: {UA}",
           "-o", "/dev/null", "-D", "-", job_url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 8)
    except Exception:
        return True   # 探不到就别误报风控（隧道问题由出口状态那条负责）
    return "verify.html" not in (out.stdout or "").lower()


def probe_job_url(con: sqlite3.Connection, override: str) -> str:
    """拿一条真实岗位 URL 给 verify_gate 用。

    默认现查库里最新一条，**不写死 job_id**：写死的那条一旦关闭下线，
    302 判据会静默给出错误答案。库不可读时退回 override（或跳过探测）。
    """
    if override:
        return override
    try:
        row = con.execute(
            "SELECT source_job_id FROM jobs WHERE retention_status='active' "
            "AND job_invalid=0 ORDER BY updated_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""
    return f"https://www.zhipin.com/job_detail/{row[0]}.html"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-cap", type=int, default=1000,
                    help="刷新链路日线，用于显示余量。改了 supervisor 的 "
                         "--refresh-cap 就要同步改这里，否则巡检报的是旧线")
    ap.add_argument("--collect-cap", type=int, default=900,
                    help="采集链路日线，同上")
    ap.add_argument("--verify-give-up", type=float, default=20.0,
                    help="人工验证窗口挂满这么多分钟就给出停止指令（默认 %(default)s）。"
                         "挂着不等于该一直等：无人处理的窗口零产出还占着出口")
    ap.add_argument("--challenge-window", type=int, default=40,
                    help="按最近 N 次取值算挑战率")
    ap.add_argument("--challenge-alarm", type=float, default=0.9,
                    help="挑战率超过它就报需人工（默认 %(default)s）")
    ap.add_argument("--challenge-warn", type=float, default=0.5,
                    help="挑战率超过它给出提醒（默认 %(default)s）")
    ap.add_argument("--only-exit", default="", help="只查这个出口")
    ap.add_argument("--skip-exit", default="", help="跳过这些出口（逗号分隔）")
    ap.add_argument("--probe-job-url", default="",
                    help="verify_gate 用的岗位 URL。默认现查库里最新一条 active")
    ap.add_argument("--no-gate", action="store_true",
                    help="跳过岗位页 302 探测（每个出口一次请求）")
    ap.add_argument("--db", default=str(DB), help="数据库路径")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    print(f"===== 巡检 {datetime.now():%H:%M:%S} =====")
    risk = False

    # ① 调度器状态。它在跑就别手工起批次。
    if STOP_FLAG.exists():
        risk = True
        # ⚠️ 已知的停机不必每轮重播全文：同一次停机连续三轮输出一字不差的四行，
        # 在 20 分钟一次的节奏下只是噪音。有信息量的是"停了多久""起因""状态变没变"。
        # 重复播报的代价和"狼来了"一样：人开始跳过巡检输出，真信号也被跳过。
        try:
            mins = (datetime.now().timestamp() - STOP_FLAG.stat().st_mtime) / 60
        except OSError:
            mins = 0.0
        lines = STOP_FLAG.read_text(encoding="utf-8").splitlines()
        cause = next((l for l in lines if "RISK-STOP" in l or "命中" in l), "")
        print(f"  ⛔ 调度器已停机 {mins:.0f} 分钟：{cause.strip() or lines[1:2]}")
        if mins < 25:
            for line in lines[:4]:
                print(f"     {line}")
        else:
            print(f"     处置指引见 {STOP_FLAG.name}；处理完删掉它我才会恢复流水线。")
    elif alive(SUPERVISOR) >= 1:
        print("  调度器  运行中   —— 续跑由它决定，**不要手工起新批次**")
    else:
        print("  调度器  未运行   —— 「已结束」的链路才需要人工判断是否续跑")

    # ② 人工验证窗口。开着时链路"已结束"是等人，不是异常，也绝不能续跑
    #    （新批次会立刻再撞验证，还会和验证窗口抢同一个出口）。
    if alive("manual_verify_proxy") >= 1:
        age = verify_age_minutes()
        if age is not None and age >= args.verify_give_up:
            risk = True
            print(f"  ⛔ 人工验证窗口已挂 {age:.0f} 分钟无人处理"
                  f"（阈值 {args.verify_give_up}）——**停止任务，不要再续窗口**：")
            print("     uv run python scripts/cleanup_browsers.py --kill")
            print("     等人醒了再弹一次即可（重弹成本远低于挂几小时）。")
        else:
            shown = f"{age:.0f} 分钟" if age is not None else "时长未知"
            print(f"  ⚠️ 人工验证窗口开着（已 {shown}）—— 等人点验证，"
                  f"**不要起任何批次**；超过 {args.verify_give_up} 分钟就停掉")

    # ③ 两条链路的进程与日志。
    for name, (needle, log) in CHAINS.items():
        n = alive(needle)
        p = latest_log(log)
        tail, hits, budget = "", [], []
        if p and p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            hits = [l for l in lines if "[RISK-STOP]" in l]
            budget = [l for l in lines if "[BUDGET-STOP]" in l]
            for l in reversed(lines):
                if re.search(r"进度|完成|本轮|待检测|待补", l):
                    tail = l.strip()[:70]
                    break
        print(f"  {name:<9} {'运行中' if n >= 1 else '已结束':<7} {tail}"
              + (f"   [{p.name}]" if p else "   [无日志]"))
        if budget and not hits:
            print(f"    💰 额度到顶正常停止（无需人工）：{budget[-1].strip()[:80]}")
        if hits:
            risk = True
            print(f"    ⛔ RISK-STOP: {hits[-1].strip()[:100]}")

    con = sqlite3.connect(args.db, timeout=30.0)

    # ④ 出口。坏了要不要叫人取决于**还有没有能用的**。
    print("  出口:")
    blocked, healthy, tunnel_down = [], [], []
    gate_url = "" if args.no_gate else probe_job_url(con, args.probe_job_url)
    try:
        pool = list(exit_pool(only=args.only_exit, skip=args.skip_exit))
    except ValueError as exc:
        print(f"    {exc}")
        pool = []
    for label, port in pool + [(DIRECT_LABEL, None)]:
        verdict, note = exit_verdict(port)
        print(f"    {label:<10} {verdict} — {note}")
        if verdict in ("verify", "blocked"):
            blocked.append(label)
        elif verdict == "dead" and port:
            tunnel_down.append((label, port))
        elif verdict == "ok" and port:
            if gate_url and not verify_gate(port, gate_url):
                print(f"    {'':<10} ⛔ 岗位页 302 → verify.html（geetest 人工层），"
                      f"状态接口尚未标记")
                blocked.append(label)
            else:
                healthy.append(label)

    if blocked and not healthy:
        risk = True
        print(f"  ⛔ 所有跳板出口都需要人工验证（{', '.join(blocked)}）——刷新链路无出口可用。")
    elif blocked:
        print(f"  ⚠️ {', '.join(blocked)} 需人工验证，但 {', '.join(healthy)} 仍可用 ——"
              f"链路能继续，不用半夜叫人。")
        print(f"     跑批时加 --only-exit {healthy[0]} 避开坏出口"
              f"（坏出口会连累好的：撞墙几次就轮换过去然后空转）。")
    if tunnel_down:
        print("  隧道不通（**不是风控**，重起即可，不需要人工过验证）:")
        for label, port in tunnel_down:
            print(f"    ssh -f -N -D {port} -o ServerAliveInterval=30 bohrium-{label}")

    # ⑤ 战果。链路在跑时才看挑战率——停机后那是历史数据。
    if alive("refresh_status_loop") >= 1:
        ok_n, ch_n = recent_challenge_rate(con, args.challenge_window)
        if ok_n + ch_n >= 10:
            rate = ch_n / (ok_n + ch_n)
            if rate >= args.challenge_alarm:
                risk = True
                print(f"  ⛔ 刷新链路最近 {ok_n+ch_n} 次取值 {ch_n} 次撞挑战页"
                      f"（{rate*100:.0f}%，ok 仅 {ok_n}）——链路实际判不出东西。")
                print("     ⚠️ **先排除隧道，别先怀疑风控**：端口 LISTENING ≠ 流量通。"
                      "SSH 转发死掉的表现就是 100% 撞挑战页。")
                print("        uv run python scripts/exit_health.py")
                print("     ⚠️ 再确认弹窗是否有效：若窗口自己漂到「BOSS直聘注册登录」，")
                print("        说明这个 IP 现在过不去（不是点不点的问题），只能等解封。")
                print("     窗口能停在验证页时才值得点（在**岗位详情页**上点，不要登录）：")
                print("     uv run python scripts/manual_verify_proxy.py "
                      "--socks 127.0.0.1:1080 --tag vm")
            elif rate >= args.challenge_warn:
                print(f"  ⚠️ 刷新链路挑战率偏高：最近 {ok_n+ch_n} 次里 {ch_n} 次"
                      f"（{rate*100:.0f}%），留意是否在滑向验证态")

    # ⑥ 额度与库存。
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
    print(f"  额度: 刷新 {r}/{args.refresh_cap}   采集 {c}/{args.collect_cap}")
    print(f"  库存: 有JD {jd}  待补详情 {left_d}  待刷 {left_r}")
    print("  " + ("⛔ 需要人工介入" if risk else "✅ 无验证/风控信号"))
    sys.exit(2 if risk else 0)


if __name__ == "__main__":
    main()
