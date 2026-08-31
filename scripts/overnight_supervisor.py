"""无人值守调度：按阶段推进，越过配额只降速不停机，撞风控立刻停。

**阶段顺序**（2026-08-31 用户指定）：
  ① 刷新 —— 把该刷新的刷完（不再按日线截断，越线降速跑）
  ② 补 JD —— 刷完之后补详情内容
  ③ 本地库优化 —— 补完跑一轮 clean_local_db + 标签 + 导出
  ④ 交叉采集 —— 标题（列表）与 JD（详情）交替推进，持续跑

**配额语义**（与旧版最大的区别）：日线不再是停线，而是**降速线**。
用户原话：「达到相关配额还没风控的话就降低速度运行，直到该刷新的刷新完毕」。
可以这么做的理由：900/1000 都不是平台给的数字，是我们按「未触发封禁的那轮」
反推的自设预算。真正的风控信号（code=36/32、验证页、出口 code=31）仍然立刻停。
降速而非原速：封禁按累计量也按速率算，越线后速率是唯一还能压的旋钮。

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
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar.refresh_fetch import DEFAULT_WINDOW_POSITION  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
STOP_FLAG = ROOT / "logs" / "SUPERVISOR-STOPPED.txt"

# 采集链路的日线口径。**从 storage 取，别在这里另写一份**：
# 免登录刷新链路的 `detail_page` 不算在内（不同 IP、不同登录态、独立风控主体）。
COLLECT_KINDS = tuple(JobStore.COLLECT_KINDS)

# 一条详情实测约 1.2 次（含 code=37 重算），与 daily_cycle.py 同一标定值。
# 只用于日志里估算，不再用来截断队列（日线已改成降速线）。
REQ_PER_DETAIL = JobStore.REQ_PER_DETAIL

# 阶段推进的判据阈值：待办降到这个数以下就算该阶段做完，进下一阶段。
# 不取 0 是因为总有少数条目会反复失败（岗位已下架但标记没落、
# raw_json 缺 securityId），卡在 0 会让流水线永远进不到下一阶段。
DONE_THRESHOLD = 15


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
    log(f"所有阶段都没活可做，睡到 {nxt:%m-%d %H:%M}（{secs/3600:.1f} 小时后）")
    # 分段睡：让 Ctrl+C / TaskStop 及时生效，也便于日志看出它还活着。
    end = time.monotonic() + secs
    while time.monotonic() < end:
        time.sleep(min(600.0, end - time.monotonic()))
        log(f"   仍在等待…（余 {max(0.0, end - time.monotonic())/3600:.1f} 小时）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-cap", type=int, default=1000)
    ap.add_argument("--collect-cap", type=int, default=900)
    ap.add_argument("--skip-fresh-hours", type=float, default=12.0)
    # 节奏默认值与 refresh_status_loop.py 保持一致，
    # 否则手工跑和调度跑用两套节奏而日志里看不出差别。
    ap.add_argument("--gap", type=float, nargs=2, default=[18.0, 32.0])
    ap.add_argument("--burst", type=int, default=25)
    ap.add_argument("--burst-pause", type=float, nargs=2, default=[90.0, 180.0])
    ap.add_argument("--rotate-every", type=int, default=15)
    ap.add_argument("--collect-config", default="config.cities3.yaml")
    ap.add_argument("--collect-limit", type=int, default=400)
    ap.add_argument("--list-pages", type=int, default=0,
                    help="交叉采集阶段每个关键词翻几页（0=按配置分词决定）")
    ap.add_argument("--cycles", type=int, default=0,
                    help="最多跑几轮，0=不限（跨日持续跑）")
    # ⚠️ 阶段顺序是**参数**，不是写死的流程。
    # 每次任务的目标不同（今晚要「刷完→补JD→优化→交叉」，下次可能只要补JD），
    # 把顺序固化进代码等于每次改需求都要改代码。
    ap.add_argument("--stages", default="refresh,backfill,optimize,cross",
                    help="阶段顺序，逗号分隔。可选 refresh/backfill/optimize/cross/tags。"
                         "按给定顺序取第一个「有活可做」的阶段执行")
    ap.add_argument("--over-quota", choices=("stop", "slow"), default="stop",
                    help="越过日线后：stop=停机等次日（默认），slow=降速跑完")
    # 出口选择是策略：某个出口撞验证时，用好的那个继续跑比整体停机划算。
    # 留空 = 全部出口（默认行为不变）。
    ap.add_argument("--only-exit", default="",
                    help="刷新阶段只用指定出口（vm / container）。"
                         "一个出口撞验证、另一个还好时用它，避免坏出口连累好的")
    ap.add_argument("--skip-exit", default="",
                    help="刷新阶段跳过这些出口（逗号分隔）")
    # 下面这些都只是透传给刷新脚本。默认值与被调脚本保持一致，
    # 否则「手工跑」和「调度跑」会用两套节奏，而日志里看不出差别。
    ap.add_argument("--slow-ladder", default="1.0:2,1.25:3,1.5:4,2.0:6",
                    help="降速档位（配合 --over-quota slow），透传给刷新脚本")
    ap.add_argument("--stale-days", type=int, default=60,
                    help="活跃度换算天数 >= 此值降级为 sample，透传")
    ap.add_argument("--include-no-jd", action="store_true",
                    help="刷新阶段也刷还没抓 JD 的岗位，透传")
    ap.add_argument("--window-position", default=DEFAULT_WINDOW_POSITION,
                    help="浏览器窗口落点 x,y（默认 %(default)s，主屏之外不抢焦点），透传")
    # ⚠️ 无人值守**默认不弹**验证窗：没人点的窗口零产出，还占着出口。
    # 但人在场时必须有路可走 —— 那就是这个开关。
    ap.add_argument("--verify", action="store_true",
                    help="允许刷新阶段撞验证时弹窗（默认不弹）。只在人确认在场时加")
    ap.add_argument("--verify-wait", type=float, default=300.0,
                    help="配合 --verify：每次弹窗等待人工的秒数")
    ap.add_argument("--done-threshold", type=int, default=DONE_THRESHOLD,
                    help=f"待办低于此数即视为该阶段做完（默认 {DONE_THRESHOLD}）。"
                         "不取 0：总有少数条目反复失败，卡在 0 会让流水线停在原地")
    args = ap.parse_args()

    if STOP_FLAG.exists():
        log(f"发现停机标记 {STOP_FLAG.name}，先处理完风控再删掉它。")
        log(STOP_FLAG.read_text(encoding="utf-8"))
        return

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    known = {"refresh", "backfill", "optimize", "cross", "tags"}
    unknown = [s for s in stages if s not in known]
    if unknown:
        log(f"未知阶段 {unknown}，可选 {sorted(known)}")
        return

    log("=" * 64)
    log("无人值守调度启动")
    log(f"  阶段顺序: {' → '.join(stages)}")
    log(f"  日线 刷新 {args.refresh_cap} / 采集 {args.collect_cap}"
        f"（越线行为 --over-quota {args.over_quota}）")
    log(f"  刷新节奏 条间 {args.gap[0]:.0f}-{args.gap[1]:.0f}s，"
        f"每 {args.burst} 条停 {args.burst_pause[0]/60:.1f}-"
        f"{args.burst_pause[1]/60:.1f} 分钟")
    log(f"  跳过 {args.skip_fresh_hours:.0f}h 内已刷过的行")
    log("  ⛔ 风控（code=36/32、验证页、出口 code=31）即整体停机，不自动重试")
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

        stamp = datetime.now().strftime("%Y%m%d")
        thr = args.done_threshold
        # 越线时是否还继续起新段。stop 模式下没余量就不起（等次日），
        # slow 模式下照起，由子脚本自己降速。
        r_ok = args.over_quota == "slow" or room_r > 0
        c_ok = args.over_quota == "slow" or room_c > 0

        # 阶段实现。每个返回 True 表示"这一轮做了事"，False 表示"没活可做，
        # 看下一个阶段"。**顺序由 --stages 决定**，这里只提供实现。
        def do_refresh() -> bool:
            if rq <= thr or not r_ok:
                return False
            if alive("refresh_status_loop") >= 1:
                log("刷新已有进程在跑，等它（共用出口会互相打死）")
                time.sleep(120)
                return True
            log(f"【刷新】待刷 {rq} 条")
            cmd = ["uv", "run", "python", "scripts/refresh_status_loop.py",
                   "--all", "--gap", str(args.gap[0]), str(args.gap[1]),
                   "--burst", str(args.burst),
                   "--burst-pause", str(args.burst_pause[0]),
                   str(args.burst_pause[1]),
                   "--rotate-every", str(args.rotate_every),
                   "--skip-fresh-hours", str(args.skip_fresh_hours),
                   "--daily-cap", str(args.refresh_cap),
                   "--over-quota", args.over_quota,
                   "--slow-ladder", args.slow_ladder,
                   "--stale-days", str(args.stale_days),
                   "--window-position", args.window_position]
            if args.only_exit:
                cmd += ["--only-exit", args.only_exit]
            if args.skip_exit:
                cmd += ["--skip-exit", args.skip_exit]
            if args.include_no_jd:
                cmd += ["--include-no-jd"]
            # 无人值守默认不弹验证窗（没人点等于白开，还占着出口）。
            # 人在场时用 --verify 显式打开。
            if args.verify:
                cmd += ["--verify", "--verify-wait", str(args.verify_wait)]
            if run_stage("refresh", ROOT / "logs" / f"refresh-slow-{stamp}.log",
                         cmd) == "risk":
                halt("refresh 命中 [RISK-STOP]")
                sys.exit(3)
            return True

        def do_backfill() -> bool:
            if cq <= thr or not c_ok:
                return False
            if alive("backfill_details") >= 1:
                log("补详情已有进程在跑，等它")
                time.sleep(120)
                return True
            lim = min(args.collect_limit, cq)
            log(f"【补JD】待补 {cq} 条，本段 {lim} 条")
            if run_stage("backfill", ROOT / "logs" / f"backfill-slow-{stamp}.log",
                         ["uv", "run", "python", "scripts/backfill_details.py",
                          "--config", args.collect_config,
                          "--limit", str(lim),
                          "--over-quota", args.over_quota]) == "risk":
                halt("backfill 命中 [RISK-STOP]")
                sys.exit(3)
            return True

        def do_tags() -> bool:
            """打标签。**不占平台配额**（只调 LLM），不受任何日线约束。"""
            if ut <= 0 or alive("extract_glm_tags") >= 1:
                return False
            log(f"【标签】{ut} 条（不占平台配额）")
            run_stage("tags", ROOT / "logs" / f"tags-{stamp}.log",
                      ["uv", "run", "python", "scripts/extract_glm_tags.py",
                       "--limit", "0", "--concurrency", "7"])
            return True

        def do_optimize() -> bool:
            """本地库优化：标签 → 降级 → 导出。全部零平台请求。

            用标记文件保证每天只做一次：后面的阶段跑完会回到这里，
            没有标记就会反复重做（导出一次十几秒，但 clean 会反复刷 updated_at）。
            """
            flag = ROOT / "logs" / f".optimized-{stamp}"
            if flag.exists():
                return False
            log("【本地优化】标签 → 按画像降级 → 导出投递池")
            do_tags()
            # **不加 --purge**：物理删除不可逆，而 sample 记录还要当增量去重样本。
            # 无人值守时更不该做不可逆的动作。
            for name, script in (("clean", "clean_local_db.py"),
                                 ("glm-filter", "apply_glm_filters.py"),
                                 ("export", "export_pool.py")):
                run_stage(name, ROOT / "logs" / f"optimize-{stamp}.log",
                          ["uv", "run", "python", f"scripts/{script}"])
            flag.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n",
                            encoding="utf-8")
            return True

        def do_cross() -> bool:
            """交叉采集：列表（标题）↔ 详情（JD）交替。

            先翻列表把新岗位灌进来，再补它们的详情 —— 这样详情永远有活可做，
            而不是把列表翻到底之后闲置。
            """
            if not c_ok:
                return False
            log("【交叉采集】列表 → 详情")
            if run_stage("expand-list", ROOT / "logs" / f"expand-{stamp}.log",
                         ["uv", "run", "python", "scripts/expand_list.py",
                          "--config", args.collect_config,
                          "--pages", str(args.list_pages),
                          "--over-quota", args.over_quota]) == "risk":
                halt("expand_list 命中 [RISK-STOP]")
                sys.exit(3)
            time.sleep(30)
            con2 = _con()
            cq2 = collect_queue(con2)
            con2.close()
            if cq2 > thr and alive("backfill_details") < 1:
                lim = min(args.collect_limit, cq2)
                log(f"  列表带来 {cq2} 条待补，接着补 {lim} 条")
                if run_stage("backfill",
                             ROOT / "logs" / f"backfill-slow-{stamp}.log",
                             ["uv", "run", "python", "scripts/backfill_details.py",
                              "--config", args.collect_config,
                              "--limit", str(lim),
                              "--over-quota", args.over_quota]) == "risk":
                    halt("backfill 命中 [RISK-STOP]")
                    sys.exit(3)
            else:
                # 列表没带来新活：当前关键词深度已翻到底。等一会再来，
                # 让平台侧出现新岗位，而不是空转刷屏。
                log(f"  列表未带来新的待补（{cq2} 条），休息 30 分钟")
                time.sleep(1800)
            return True

        impls = {"refresh": do_refresh, "backfill": do_backfill,
                 "optimize": do_optimize, "cross": do_cross, "tags": do_tags}

        # 按 --stages 的顺序取第一个「有活可做」的阶段。
        # 每轮只做一个阶段然后重新查库：库状态会被子进程改变，
        # 沿用轮次开头的快照做后续决策等于拿过期数据下判断。
        if not any(impls[s]() for s in stages):
            # 所有阶段都没活可做。stop 模式下这通常意味着额度用尽 → 睡到次日；
            # slow 模式下意味着真的做完了 → 也睡，让平台侧出现新岗位。
            sleep_until_tomorrow()
        else:
            time.sleep(60)


if __name__ == "__main__":
    main()
