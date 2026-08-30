"""无人值守调度：刷新 → 补详情 → 打标签，额度用尽就睡到次日，撞风控就停。

**为什么需要它**：两条链路各有独立日线（刷新 1000 次 `detail_page`、采集 900 次
`COLLECT_KINDS`），而它们几乎从不同时到顶 —— 手工跑就得守着看谁先空出来。
夜里没人守，就需要一个进程替人做「查余量 → 起下一段 → 到顶就睡」这件事。

**它不做的事**（这些是它安全的前提）：
  · 不改任何节奏参数、不改配置 —— 只按给定参数起子进程
  · 不在撞风控后重试。`[RISK-STOP]` 一出现就整体停机并留标记文件，
    等人来处理。自动重试风控是把一次可恢复的限制熬成硬封。
  · 不碰 `detail_cooldown_seconds`（用户明确要求保持不动）

**判据全部现场查库**，不读日志里的历史结论 —— 日志是快照，会过期。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
STOP_FLAG = ROOT / "logs" / "SUPERVISOR-STOPPED.txt"

# 采集链路的日线口径。与 storage.JobStore.COLLECT_KINDS 一致：
# 免登录刷新链路的 `detail_page` **不算在内**（不同 IP、不同登录态、独立风控主体）。
COLLECT_KINDS = ("list", "detail", "security_check", "recommend")

# 一条刷新摊薄 1.6 次请求（1 次 fetch + 每 5 条一次 3 次重算）。
# 从 refresh_fetch 取而不是抄字面值：那边是这个常数的唯一出处。
from job_radar.refresh_fetch import REQ_PER_JOB_AMORTIZED  # noqa: E402

# 一条详情实测约 1.2 次（含 code=37 重算），与 daily_cycle.py 同一标定值。
REQ_PER_DETAIL = 1.2

# 低于这个余量就不起新一段：起一段的固定开销（冷启动暖 stoken 3 次、
# 采集侧导航建域）会把零头吃掉，反而留下一个刚启动就 BUDGET-STOP 的日志。
MIN_ROOM_REFRESH = 40
MIN_ROOM_COLLECT = 60


def log(msg: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def _con() -> sqlite3.Connection:
    # timeout=30：三条链路并行写同一个库，默认 5s 在争抢下会抛 database is locked。
    con = sqlite3.connect(DB, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def today() -> str:
    """本地日历日。**不用 UTC**：平台的解封时间按北京时间给，
    用 UTC 会让「今天」在早上 8 点前错位到前一天，把昨天的额度算进今天。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def used_refresh(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT count(*) FROM request_log WHERE day=? AND kind='detail_page'",
        (today(),)).fetchone()[0]


def used_collect(con: sqlite3.Connection) -> int:
    qs = ",".join("?" * len(COLLECT_KINDS))
    return con.execute(
        f"SELECT count(*) FROM request_log WHERE day=? AND kind IN ({qs})",
        (today(), *COLLECT_KINDS)).fetchone()[0]


def refresh_queue(con: sqlite3.Connection, skip_hours: float) -> int:
    """待刷条数，**与 refresh_status_loop 的选行条件同口径**。

    包含跳过「N 小时内已刷过」那一条：不含它会一直报几百条待刷，
    而子进程起来后实际选出几条就退出 —— 调度器于是空转刷屏。
    """
    return con.execute(
        "SELECT count(*) FROM jobs WHERE retention_status='active' "
        "AND job_invalid=0 AND job_status_desc != '职位已关闭' "
        "AND job_description != '' "
        "AND (updated_at IS NULL OR updated_at = '' OR "
        f"     updated_at < datetime('now','-{skip_hours} hours'))"
    ).fetchone()[0]


def collect_queue(con: sqlite3.Connection) -> int:
    """待补详情条数（**已过投递池判定**）。

    必须用 `should_demote` 过一遍，否则会把注定被降级的行算成待办：
    实测存量里约 4 成注定降级，一条详情一次不可退的请求。
    """
    from clean_local_db import should_demote
    rows = con.execute(
        "SELECT * FROM jobs WHERE job_description='' AND raw_json!='{}' "
        "AND job_invalid=0 AND retention_status='active'").fetchall()
    return sum(1 for r in rows if not should_demote(r)[0])


def untagged(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT count(*) FROM jobs WHERE job_description!='' "
        "AND retention_status='active' AND glm_tags_updated_at=''").fetchone()[0]


def alive(needle: str) -> int:
    """数匹配 CommandLine 的 python 进程。uv 启动器会多算一个，所以 >=1 即算活。

    ⚠️ 起新一段前**必须**查：两个刷新进程共用同一组出口会互相把 stoken 打掉。
    """
    ps = (f"(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{needle}*' }}).Count")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def run_stage(name: str, log_path: Path, cmd: list[str]) -> str:
    """跑一段，实时转发并落盘。返回 'ok' / 'budget' / 'risk' / 'error'。

    ⚠️ 必须逐行转发而不是让子进程继承 stdout：重定向到文件时子进程的 stdout
    变成全缓冲，几十分钟的日志会攒在缓冲区里不落盘，外面看着像卡死。
    """
    log(f"起 {name}: {' '.join(cmd[3:])}")
    log(f"   日志 {log_path.name}")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    verdict = "ok"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {name} 启动 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            fh.flush()
            # 只认这两个标记来判定停止原因。**不看最后一行**：
            # 长跑日志末尾常是节奏提示，看起来像卡住其实正常。
            if "[RISK-STOP]" in line:
                verdict = "risk"
            elif "[BUDGET-STOP]" in line and verdict != "risk":
                verdict = "budget"
        rc = proc.wait()
    if verdict == "risk":
        log(f"⛔ {name} 命中风控标记")
        return "risk"
    if rc != 0:
        log(f"⚠️ {name} 退出码 {rc}")
        return "error" if verdict == "ok" else verdict
    log(f"✅ {name} 结束（{verdict}）")
    return verdict


def halt(reason: str) -> None:
    """写停机标记并退出。标记文件是给巡检读的 —— 它比翻日志可靠。"""
    STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.write_text(
        f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{reason}\n"
        "调度器已停机，不会自动重试。风控需要人工处理：\n"
        "  账号: uv run python scripts/check_account_status.py\n"
        "  出口: uv run python scripts/manual_verify_proxy.py "
        "--socks 127.0.0.1:<port> --tag <名>\n",
        encoding="utf-8")
    log(f"⛔ 停机：{reason}")
    log(f"⛔ 标记已写入 {STOP_FLAG}")


def sleep_until_tomorrow() -> None:
    """睡到次日本地零点后 3 分钟（让账本的 day 确实翻页再起）。"""
    now = datetime.now().astimezone()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=3, second=0,
                                            microsecond=0)
    secs = max(60.0, (nxt - now).total_seconds())
    log(f"两条链路都到顶，睡到 {nxt:%m-%d %H:%M}（{secs/3600:.1f} 小时后）")
    # 分段睡，让 Ctrl+C / TaskStop 能及时生效，也便于日志看出它还活着。
    end = time.monotonic() + secs
    while time.monotonic() < end:
        time.sleep(min(600.0, end - time.monotonic()))
        log(f"   仍在等待日线重置…（余 {(end-time.monotonic())/3600:.1f} 小时）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-cap", type=int, default=1000)
    ap.add_argument("--collect-cap", type=int, default=900)
    ap.add_argument("--skip-fresh-hours", type=float, default=12.0)
    ap.add_argument("--gap", type=float, nargs=2, default=[15.0, 25.0])
    ap.add_argument("--burst", type=int, default=25)
    ap.add_argument("--burst-pause", type=float, nargs=2, default=[90.0, 150.0])
    ap.add_argument("--rotate-every", type=int, default=15)
    ap.add_argument("--collect-config", default="config.cities3.yaml")
    ap.add_argument("--collect-limit", type=int, default=400)
    ap.add_argument("--cycles", type=int, default=0,
                    help="最多跑几轮，0=不限（跨日持续跑）")
    args = ap.parse_args()

    if STOP_FLAG.exists():
        log(f"发现停机标记 {STOP_FLAG.name}，先处理完风控再删掉它。")
        log(STOP_FLAG.read_text(encoding="utf-8"))
        return

    log("=" * 64)
    log("无人值守调度启动")
    log(f"  刷新日线 {args.refresh_cap}   采集日线 {args.collect_cap}")
    log(f"  刷新节奏 条间 {args.gap[0]:.0f}-{args.gap[1]:.0f}s，"
        f"每 {args.burst} 条停 {args.burst_pause[0]/60:.1f}-"
        f"{args.burst_pause[1]/60:.1f} 分钟")
    log(f"  跳过 {args.skip_fresh_hours:.0f}h 内已刷过的行")
    log("  ⛔ 撞 [RISK-STOP] 即整体停机，不自动重试")
    log("=" * 64)

    cycle = 0
    while True:
        cycle += 1
        if args.cycles and cycle > args.cycles:
            log(f"已跑满 {args.cycles} 轮，退出。")
            return

        con = _con()
        ur, uc = used_refresh(con), used_collect(con)
        rq = refresh_queue(con, args.skip_fresh_hours)
        cq = collect_queue(con)
        ut = untagged(con)
        con.close()

        room_r = max(0, args.refresh_cap - ur)
        room_c = max(0, args.collect_cap - uc)
        log(f"--- 第 {cycle} 轮 | 刷新 {ur}/{args.refresh_cap}（余 {room_r}）"
            f" 采集 {uc}/{args.collect_cap}（余 {room_c}）"
            f" | 待刷 {rq} 待补 {cq} 待标 {ut}")

        did = False
        stamp = datetime.now().strftime("%Y%m%d")

        # ① 刷新。放前面是因为它更便宜（1.6 次/条 vs 1.2 次/条但采集还要翻列表），
        #    而且它的产出（关闭/不活跃判定）直接决定投递池的准确性。
        if room_r >= MIN_ROOM_REFRESH and rq > 0:
            n = alive("refresh_status_loop")
            if n >= 1:
                log(f"刷新已有 {n} 个进程在跑，跳过（两个进程共用出口会互相打死）")
            else:
                afford = int(room_r / REQ_PER_JOB_AMORTIZED)
                log(f"刷新可跑 ≈{min(afford, rq)} 条")
                v = run_stage(
                    "refresh", ROOT / "logs" / f"refresh-slow-{stamp}.log",
                    ["uv", "run", "python", "scripts/refresh_status_loop.py",
                     "--all", "--gap", str(args.gap[0]), str(args.gap[1]),
                     "--burst", str(args.burst),
                     "--burst-pause", str(args.burst_pause[0]),
                     str(args.burst_pause[1]),
                     "--rotate-every", str(args.rotate_every),
                     "--skip-fresh-hours", str(args.skip_fresh_hours),
                     "--daily-cap", str(args.refresh_cap)])
                if v == "risk":
                    halt("refresh 命中 [RISK-STOP]")
                    return
                did = True
        elif rq == 0:
            log("刷新队列空（都在新鲜期内），跳过")
        else:
            log(f"刷新余量 {room_r} < {MIN_ROOM_REFRESH}，今日不再起")

        # ② 补详情。
        if room_c >= MIN_ROOM_COLLECT and cq > 0:
            n = alive("backfill_details")
            if n >= 1:
                log(f"补详情已有 {n} 个进程在跑，跳过")
            else:
                afford = int(room_c / REQ_PER_DETAIL)
                lim = min(args.collect_limit, afford, cq)
                log(f"补详情可跑 ≈{lim} 条")
                v = run_stage(
                    "backfill", ROOT / "logs" / f"backfill-slow-{stamp}.log",
                    ["uv", "run", "python", "scripts/backfill_details.py",
                     "--config", args.collect_config, "--limit", str(lim)])
                if v == "risk":
                    halt("backfill 命中 [RISK-STOP]")
                    return
                did = True
        elif cq == 0:
            log("待补详情队列空，跳过")
        else:
            log(f"采集余量 {room_c} < {MIN_ROOM_COLLECT}，今日不再起")

        # ③ 打标签。**不占平台配额**（只调 LLM），所以不受上面两条日线约束。
        con = _con()
        ut = untagged(con)
        con.close()
        if ut > 0 and alive("extract_glm_tags") < 1:
            log(f"打标签 {ut} 条（不占平台配额）")
            run_stage("tags", ROOT / "logs" / f"tags-{stamp}.log",
                      ["uv", "run", "python", "scripts/extract_glm_tags.py",
                       "--limit", "0", "--concurrency", "7"])
            did = True

        if not did:
            sleep_until_tomorrow()
        else:
            # 段间小憩：让库的写事务落定，也避免两段紧邻起把出口压出尖峰。
            time.sleep(60)


if __name__ == "__main__":
    main()
