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
# 任意一个真实岗位页，只用来看服务端把它 302 到哪里（见 verify_gate）。
# 用固定 ID 而不是现查库：这条探测必须在库不可读时也能跑。
PROBE_JOB_URL = "https://www.zhipin.com/job_detail/5bfc15fbdf3480570nB-2dq0E1NX.html"

# 日志按 glob 取**最新一份**，不写死日期。
# 写死日期的代价实测过：跨零点续跑后，巡检还在读前一天的日志，
# 于是持续汇报昨天那条已经处理完的 BUDGET-STOP，而看不到新批次的进度。
# 「写死的标识符是定时炸弹」—— 它不报错，只会一直给出过期答案。
CHAINS = {
    "refresh": ("refresh_status_loop", "logs/refresh-slow-*.log"),
    "backfill": ("backfill_details", "logs/backfill-slow-*.log"),
}

# 无人值守调度器。它在两段之间会有几十秒到几分钟的空档，
# 此时两条链路都显示「已结束」——⚠️ **那不是可以手工续跑的信号**：
# 手工起一批会和调度器下一段撞成两个同名进程，共用出口互相把 stoken 打掉。
# 所以巡检必须先报「调度器在跑」，把续跑决定交给它。
SUPERVISOR = "overnight_supervisor"
STOP_FLAG = ROOT / "logs" / "SUPERVISOR-STOPPED.txt"

# 人工验证窗口挂多久就该放弃（分钟）。用户 2026-08-31 定的：
# 「单轮 20min 没有验证就可以停止任务，避免浪费」。
VERIFY_GIVE_UP_MIN = 20.0


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


def recent_challenge_rate(con: sqlite3.Connection, n: int = 40) -> tuple[int, int]:
    """最近 n 次**取值**里 (ok, challenged) 各多少。

    ⚠️ 这是判断"刷新链路还能不能干活"的**唯一可靠判据**，比任何探测都准，
    因为它就是链路自己的战果记录。
    2026-08-31 实测踩到：`forbidden-info` 回 code:0、冷 curl 也只看到
    `security.html`（可自解那层），巡检因此报 ✅——而同期账本里最近 40 次
    取值是 0 ok / 40 challenged，链路实际一条都判不出来。
    冷 curl 不带 stoken，**探不出"有会话之后会被弹到 verify.html"**，
    所以它天然无法回答这个问题；账本能。
    """
    row = con.execute(
        "SELECT COALESCE(SUM(outcome='ok'),0), COALESCE(SUM(outcome='challenged'),0) "
        "FROM (SELECT outcome FROM request_log WHERE kind='detail_page' "
        "      AND outcome IN ('ok','challenged') ORDER BY id DESC LIMIT ?)", (n,)
    ).fetchone()
    return int(row[0]), int(row[1])


def verify_gate(port: int, timeout: int = 25) -> bool:
    """岗位页是否**不需要**人工验证。True=可用，False=撞上 verify.html。

    为什么要单独探这个：`forbidden-info` 有滞后。2026-08-31 实测两个出口
    整整一小时把岗位页 302 到 `/web/passport/zp/verify.html`，而同期
    `forbidden-info` 一直回 `code:0`，巡检因此报"无风控信号"，
    刷新链路那一小时的 136 次请求全部作废。

    `verify.html` 是 geetest 人工层，与可自解的 `security-check.html`
    **不是一回事**：后者纯计算、脚本自己能过；前者必须人点，静置不会恢复。
    所以判据只看 Location 里是不是 `verify.html`。

    用 `-o /dev/null -D -` 只取响应头：一次请求、不下载正文，成本最低。
    """
    cmd = ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
           "--max-time", str(timeout), "-H", f"User-Agent: {UA}",
           "-o", "/dev/null", "-D", "-", PROBE_JOB_URL]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 8)
    except Exception:
        return True   # 探不到就别误报风控（隧道问题由 exit_state 那条负责）
    head = (out.stdout or "").lower()
    if "verify.html" in head:
        return False
    # security.html（可自解那层）不算需要人工 —— 脚本自己会过。
    return True


def main() -> None:
    print(f"===== 巡检 {datetime.now():%H:%M:%S} =====")

    risk = False
    sup = alive(SUPERVISOR)
    if STOP_FLAG.exists():
        risk = True
        print("  ⛔ 调度器已停机（撞风控），标记内容：")
        for line in STOP_FLAG.read_text(encoding="utf-8").splitlines()[:4]:
            print(f"     {line}")
    elif sup >= 1:
        print("  调度器  运行中   —— 续跑由它决定，**不要手工起新批次**")
    else:
        print("  调度器  未运行   —— 「已结束」的链路才需要人工判断是否续跑")
    # 人工验证窗口正开着：这时链路"已结束"是**等人**，不是异常，也绝不能续跑
    # （新批次会立刻再撞验证，还会和验证窗口抢同一个出口）。
    #
    # ⚠️ 但**开着不等于该一直等**（用户 2026-08-31 要求）：
    # 「单轮 20min 没有验证就可以停止任务，避免浪费」。
    # 实测代价：那次挂了 4 小时窗口，人在睡觉，12 轮巡检全在重复"仍待验证"，
    # 窗口零产出还占着出口。所以这里按窗口存活时长给出**明确的停止指令**，
    # 而不是让下一个巡检自己判断"要不要再续一轮"。
    nverify = alive("manual_verify_proxy")
    if nverify >= 1:
        age = verify_age_minutes()
        if age is not None and age >= VERIFY_GIVE_UP_MIN:
            print(f"  ⛔ 人工验证窗口已挂 {age:.0f} 分钟无人处理"
                  f"（阈值 {VERIFY_GIVE_UP_MIN}）——**停止任务，不要再续窗口**：")
            print("     powershell -NoProfile -Command \"Get-CimInstance Win32_Process "
                  "-Filter \\\"Name='python.exe'\\\" | Where-Object "
                  "{ $_.CommandLine -like '*manual_verify_proxy*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\"")
            print("     等人醒了再弹一次即可（重弹成本远低于挂几小时）。")
            risk = True
        else:
            shown = f"{age:.0f} 分钟" if age is not None else "时长未知"
            print(f"  ⚠️ 人工验证窗口开着（已 {shown}）—— 等人点验证，"
                  f"**不要起任何批次**；超过 {VERIFY_GIVE_UP_MIN} 分钟就停掉")

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
    blocked_exits: list[str] = []
    healthy_exits: list[str] = []
    for label, port in (("vm", 1080), ("container", 1082), ("家宽", None)):
        s = exit_state(port)
        print(f"    {label:<10} {s}")
        if s.startswith("⛔"):
            # ⚠️ **先记账，别立刻判 risk**：只要还有一个出口能用、链路也在
            # 正常产出，那"某个备用出口坏了"就不是需要半夜叫人的事。
            # 2026-08-31 实测踩到：vm 撞 verify 而 container 正常、
            # 刷新进度在递增（10/1119、活跃度 3），巡检仍每 20 分钟报一次
            # "⛔ 需要人工介入" —— 那是狼来了，会把真正要人的信号淹掉。
            # 判据在下面统一算：**所有出口都坏 → ⛔；还剩好的 → ⚠️**。
            blocked_exits.append(label)
        elif s.startswith("ok") and port:
            healthy_exits.append(label)
        if s.startswith("⚠️") and port:
            tunnel_down.append((label, port))   # 隧道断 → 重起即可，不用人
        elif port and s.startswith("ok") and not verify_gate(port):
            # ⚠️ **`forbidden-info` 回 code:0 不等于出口能用**（2026-08-31 实测）：
            # 两个出口整整一小时把岗位页 302 到 `verify.html`（geetest 人工层），
            # 而 forbidden-info 全程 code:0 —— 巡检因此一路报"✅ 无风控信号"，
            # 直到一小时后平台才在 forbidden-info 里也标记出来。
            # 那一小时里刷新链路的 ok 归零、136 次请求全废。
            # 判据必须**直接问岗位页要不要人过验证**，不能只信状态接口。
            print(f"    {'':<10} ⛔ 岗位页 302 → verify.html（geetest 人工层），"
                  f"forbidden-info 尚未标记")
            blocked_exits.append(label)
            if label in healthy_exits:
                healthy_exits.remove(label)

    # 出口坏了要不要叫人，取决于**还有没有能用的**。
    if blocked_exits and not healthy_exits:
        risk = True
        print(f"  ⛔ 所有跳板出口都需要人工验证（{', '.join(blocked_exits)}）——"
              f"刷新链路无出口可用。")
    elif blocked_exits:
        print(f"  ⚠️ {', '.join(blocked_exits)} 需人工验证，但 "
              f"{', '.join(healthy_exits)} 仍可用 —— 链路能继续，不用半夜叫人。")
        print(f"     跑批时加 --only-exit {healthy_exits[0]} 避开坏出口"
              f"（坏出口会连累好的：撞墙 3 次就轮换过去然后空转）。")
    if tunnel_down:
        print("  隧道不通（**不是风控**，重起即可，不需要人工过验证）:")
        for label, port in tunnel_down:
            print(f"    ssh -f -N -D {port} -o ServerAliveInterval=30 "
                  f"bohrium-{'vm' if label == 'vm' else 'container'}")

    con = sqlite3.connect(DB, timeout=30.0)
    # ⚠️ 战果判据：出口"看起来干净"不代表链路能干活。
    # 最近一批取值全是 challenged 就是**在等人过验证**，哪怕
    # forbidden-info 回 code:0、冷 curl 只看到 security.html。
    ok_n, ch_n = recent_challenge_rate(con)
    if ok_n + ch_n >= 10:
        rate = ch_n / (ok_n + ch_n)
        if rate >= 0.9:
            risk = True
            print(f"  ⛔ 刷新链路最近 {ok_n+ch_n} 次取值 {ch_n} 次撞挑战页"
                  f"（{rate*100:.0f}%，ok 仅 {ok_n}）——链路实际判不出东西。")
            # ⚠️ **先查隧道，别先怀疑风控**（2026-08-31 实测的根因）：
            # vm 的 SSH 转发死了（端口仍 LISTENING 但流量不通，curl 20s 超时），
            # 表现就是 100% 撞挑战页 —— 而 forbidden-info 走同一条死隧道
            # 拿不到响应、又被旧代码当成 code:0，于是看着像"被限制"。
            # 我因此弹了两轮无效验证窗口、白等了 8 小时。
            # 重起隧道后同一条 fetch 路径立刻 4/4 ok。
            print("     ⚠️ 先排除隧道：端口 LISTENING ≠ 流量通。逐个试")
            print("        curl -sS --socks5-hostname 127.0.0.1:1080 "
                  "--max-time 20 -o /dev/null -w '%{http_code}\\n' "
                  "https://www.zhipin.com/")
            print("        超时就重起（先杀掉旧 ssh.exe，否则端口占用起不来）：")
            print("        ssh -f -N -D 1080 -o ServerAliveInterval=30 bohrium-vm")
            print("     出口状态接口可能仍回 code:0（它有滞后），别据此当正常。")
            # ⚠️ **别无条件建议弹窗**：2026-08-31 实测有一种状态是弹了也过不去 ——
            # 免登录访问岗位页被直接推到**登录页**（窗口无人操作也会自己漂过去），
            # 页面根本到不了验证那一步，给人点也没有可点的东西。
            # 那次连弹两轮全部白费，正是用户说的"浪费"。
            # 判据：先看窗口能不能停在验证页，停不住就只能等平台的解封时间。
            print("     ⚠️ 先确认弹窗是否有效：若窗口自己漂到「BOSS直聘注册登录」，")
            print("        说明这个 IP 现在过不去（不是点不点的问题），只能等解封，")
            print("        别反复弹窗浪费。解封时间见上面出口行的「解封」字段。")
            print("     窗口能停在验证页时才值得点（在**岗位详情页**上点，不要登录）：")
            print("     uv run python scripts/manual_verify_proxy.py "
                  "--socks 127.0.0.1:1080 --tag vm")
            print("     uv run python scripts/manual_verify_proxy.py "
                  "--socks 127.0.0.1:1082 --tag container")
        elif rate >= 0.5:
            print(f"  ⚠️ 刷新链路挑战率偏高：最近 {ok_n+ch_n} 次里 {ch_n} 次"
                  f"（{rate*100:.0f}%），留意是否在滑向验证态")
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
