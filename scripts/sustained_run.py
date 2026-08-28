"""持续采集：用激进速率长时间跑，看能稳定维持多久。

**和 probe_ban_boundary.py 的区别**：那个是"抓到被封为止"，素材一耗尽就结束。
这个的目标是**可持续性**——不主动撞红线，而是：

1. **列表和详情交替**：列表产生新素材（新岗位），详情消耗素材。
   单跑详情会在素材耗尽时停摆（实测 10 分钟就见底），交替才能一直有活干。
2. **同 profile 必须串行**：Chrome 对同一 user-data-dir 有 SingletonLock，
   两个 `launch_persistent_context` 并存会直接 `TargetClosedError`
   （2026-08-11 实测，旧记忆"并发上限 2"是错的）。所以这里严格顺序调子进程。
3. **每轮落一份心跳**：时间、累计请求、code=37 数、新增岗位、速率。
   跑断了也能从 `data/calibration/sustained-*.jsonl` 看出断在哪、当时什么状态。
4. **只在真封禁时停**（code=32/36 或滑块页）。code=37 不停——它是无记忆的
   配额窗口信号，不是封禁前兆（历史：156 条 1 次 code=37 就封 vs 108 条
   21 次零封禁，相差 20 倍结果相反）。

用法：
    python scripts/sustained_run.py                  # 一直跑到被封或手动停
    python scripts/sustained_run.py --hours 4        # 跑 4 小时
    python scripts/sustained_run.py --day-cap 1500   # 自定日线（默认读 config）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

OUT_DIR = ROOT / "data" / "calibration"
# 真封禁的标志。code=37 不在此列（可自动恢复，不是封禁）。
HARD_BAN_RE = re.compile(r"code=3[256]|已暂时被禁止|请完成.{0,4}验证|滑块")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def requests_last_hour(cfg) -> int:
    """最近 60 分钟的请求数（滚动窗口，不是整点桶）。

    分时配速用滚动窗口而非"本小时内"：整点桶会让 59 分和 01 分之间可以突发
    两倍额度，恰好制造出机器特征。ts 是本地时间字符串（storage.log_request
    统一写本地时间），datetime('now','localtime') 同一口径可直接比较。
    """
    with JobStore(cfg.database_path) as store:
        return store.conn.execute(
            "SELECT COUNT(*) FROM request_log "
            "WHERE ts >= datetime('now','localtime','-60 minutes')"
        ).fetchone()[0]


def snapshot(cfg) -> dict:
    """当前累计量快照。"""
    with JobStore(cfg.database_path) as store:
        counts = store.request_counts()
        q = lambda s: store.conn.execute(s).fetchone()[0]
        return {
            "requests_today": counts.get("total", 0),
            "by_kind": {k: v for k, v in counts.items() if k != "total"},
            "code37_today": store.conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE day = date('now','localtime') "
                "AND outcome = 'code37_recover'").fetchone()[0],
            "jobs": q("SELECT COUNT(*) FROM jobs"),
            "active": q("SELECT COUNT(*) FROM jobs WHERE retention_status='active'"),
            "with_jd": q("SELECT COUNT(*) FROM jobs "
                         "WHERE trim(coalesce(job_description,'')) <> ''"),
            "pending_detail": q(
                "SELECT COUNT(*) FROM jobs "
                "WHERE trim(coalesce(job_description,'')) = '' "
                "AND raw_json != '{}' AND job_invalid = 0"),
        }


def run_stage(script: str, *args: str) -> tuple[int, bool, str]:
    """跑一个子脚本，实时转发输出。返回 (退出码, 是否真封禁, 命中的文案)。

    必须逐行读而不是继承 stdout：重定向到文件时子进程 stdout 变全缓冲，
    长时间运行的日志会一直攒在缓冲区不落盘，外部看着像卡死。
    """
    cmd = ["uv", "run", "python", str(ROOT / "scripts" / script), *args]
    log(f"$ {script} {' '.join(args)}")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    banned, hit = False, ""
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith(("warning:", "Uninstalled", "Installed")):
            continue
        if not line:
            continue
        m = HARD_BAN_RE.search(line)
        if m:
            banned, hit = True, line[:200]
        print(f"    │ {line}", flush=True)
    return proc.wait(), banned, hit


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.aggressive.yaml")
    ap.add_argument("--hours", type=float, default=0.0,
                    help="跑多少小时后收工（0 = 不限，直到被封或手动停）")
    ap.add_argument("--day-cap", type=int, default=0,
                    help="覆盖配置里的单日上限（0 = 用配置值）")
    ap.add_argument("--list-pages", type=int, default=10)
    ap.add_argument("--detail-batch", type=int, default=60,
                    help="每轮补多少条详情")
    ap.add_argument("--per-hour", type=int, default=0,
                    help="每滚动小时的请求上限（0=不限）。把日预算摊到全天，"
                         "而不是几小时内跑完——真人不会连续高速翻一整夜")
    ap.add_argument("--wait-on-cap", action="store_true",
                    help="触及日线时不退出，睡到次日零点继续（跨天连续跑）")
    ap.add_argument("--ratchet", type=int, default=0,
                    help="跑满日线且零封禁时，把日线抬高这么多再继续（0=不抬）。"
                         "阶梯式探边界：每次只多走一小步，被封时就知道上一台阶是安全的")
    ap.add_argument("--ratchet-max", type=int, default=3000,
                    help="日线抬升的绝对上限，兜底防跑飞")
    args = ap.parse_args()

    cfg = load_config(args.config)
    day_cap = args.day_cap or cfg.max_requests_per_day
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hb_path = OUT_DIR / f"sustained-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    t0 = time.monotonic()
    start = snapshot(cfg)
    log("=" * 62)
    log(f"持续采集开始。日线 {day_cap}，"
        f"{'限时 %.1f 小时' % args.hours if args.hours else '不限时'}")
    log(f"起点：请求 {start['requests_today']} | 岗位 {start['jobs']} "
        f"| 有JD {start['with_jd']} | 待补 {start['pending_detail']}")
    log(f"速率参数：延迟 {cfg.detail_delay_seconds}、批 {cfg.detail_batch}、"
        f"冷却 {cfg.detail_cooldown_seconds}")
    log("=" * 62)

    cycle = 0
    stop_reason = "手动停止"
    ladder: list[dict] = []   # 日线抬升轨迹，被封时这就是"安全台阶"的证据
    try:
        while True:
            cycle += 1
            snap = snapshot(cfg)
            elapsed_h = (time.monotonic() - t0) / 3600

            # ---- 停止条件 ----
            if args.hours and elapsed_h >= args.hours:
                stop_reason = f"达限时 {args.hours} 小时"
                break
            if snap["requests_today"] >= day_cap:
                # 阶梯抬升：跑满当前日线且全程零真封禁，说明这个量级安全，
                # 抬一档继续。每次只走一小步——被封时就知道"上一个台阶是安全的、
                # 这一个不是"，这比一次冲到 3000 然后只知道"某处被封了"有用得多。
                if args.ratchet and day_cap < args.ratchet_max:
                    prev = day_cap
                    day_cap = min(day_cap + args.ratchet, args.ratchet_max)
                    ladder.append({"from": prev, "to": day_cap,
                                   "at_requests": snap["requests_today"],
                                   "code37": snap["code37_today"],
                                   "at": datetime.now().astimezone().isoformat(
                                       timespec="seconds")})
                    log(f"★ 跑满 {prev} 且零封禁 → 日线抬到 {day_cap}"
                        f"（上限 {args.ratchet_max}）")
                    continue
                if not args.wait_on_cap:
                    stop_reason = f"达单日上限 {day_cap}"
                    break
                # 跨天续跑：睡到次日零点后账本自然归零（day 按本地日算）
                now = datetime.now().astimezone()
                nxt = (now + timedelta(days=1)).replace(
                    hour=0, minute=3, second=0, microsecond=0)
                secs = max(60, (nxt - now).total_seconds())
                log(f"达日线 {day_cap}，睡 {secs / 3600:.1f} 小时到次日继续"
                    f"（{nxt.strftime('%m-%d %H:%M')}）")
                time.sleep(secs)
                start = snapshot(cfg)   # 新的一天，会话基线重置
                t0 = time.monotonic()
                continue

            # ---- 分时配速：把日预算摊到全天 ----
            # 不做这个的话，1000 的日线按 300 条/小时会在 3 小时内跑完，
            # 剩下 21 小时账号完全静默 —— 这个"脉冲式"作息本身就是机器特征，
            # 而且测不出"连续一天稳定"这件事。
            if args.per_hour:
                used_1h = requests_last_hour(cfg)
                if used_1h >= args.per_hour:
                    wait = 300.0
                    log(f"滚动小时已用 {used_1h}/{args.per_hour}，等 {wait / 60:.0f} 分钟")
                    time.sleep(wait)
                    continue

            done_reqs = snap["requests_today"] - start["requests_today"]
            rate = done_reqs / max(elapsed_h, 1e-9)
            log(f"── 第 {cycle} 轮 | 已跑 {elapsed_h * 60:.0f} 分钟 "
                f"| 本次会话请求 {done_reqs}（{rate:.0f}/小时）"
                f"| 今日 {snap['requests_today']}/{day_cap} "
                f"| code37 {snap['code37_today']} | 待补 {snap['pending_detail']}")

            # ---- 素材不足就先跑列表补货 ----
            # 列表和详情共享同一配额窗口（2026-08-11 实测），所以列表不是"免费"的，
            # 但它产出新岗位，是唯一能让详情一直有活干的来源。
            if snap["pending_detail"] < args.detail_batch:
                _, banned, hit = run_stage(
                    "expand_list.py", "--config", args.config,
                    # dry-stop 不写死，用 expand_list 的默认值（5）。
                    # 原先写 3 会在"浅层共享、深层独有"的第 3 页零新增处掉头，
                    # 恰好错过第 4 页开始的富矿区（见 expand_list 里的实测数据）。
                    "--pages", str(args.list_pages))
                if banned:
                    stop_reason = f"列表阶段命中真封禁：{hit}"
                    break
                snap = snapshot(cfg)
                if snap["pending_detail"] == 0:
                    stop_reason = "列表也挖不出新素材了（关键词已翻到底）"
                    break

            # ---- 补详情 ----
            # 批量要同时受日线和滚动小时约束。只在轮次开头查一次是不够的：
            # 一批 50 条 ≈ 60 次请求，若小时额度只剩 10 次，这一批就会直接冲破。
            limits = [args.detail_batch, snap["pending_detail"],
                      max(1, int((day_cap - snap["requests_today"]) / 1.2))]
            if args.per_hour:
                limits.append(max(1, int((args.per_hour - requests_last_hour(cfg)) / 1.2)))
            n = min(limits)
            if n <= 0:
                stop_reason = f"额度不足（今日 {snap['requests_today']}/{day_cap}）"
                break
            _, banned, hit = run_stage(
                "backfill_details.py", "--config", args.config,
                "--limit", str(n), "--include-sample")
            if banned:
                stop_reason = f"详情阶段命中真封禁：{hit}"
                break

            # ---- 心跳落盘 ----
            after = snapshot(cfg)
            hb = {
                "cycle": cycle,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "elapsed_minutes": round((time.monotonic() - t0) / 60, 1),
                "requests_today": after["requests_today"],
                "requests_this_session": after["requests_today"] - start["requests_today"],
                "code37_today": after["code37_today"],
                "jobs": after["jobs"],
                "jobs_gained": after["jobs"] - start["jobs"],
                "with_jd": after["with_jd"],
                "jd_gained": after["with_jd"] - start["with_jd"],
                "pending_detail": after["pending_detail"],
                "by_kind": after["by_kind"],
            }
            with hb_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(hb, ensure_ascii=False) + "\n")
    except KeyboardInterrupt:
        stop_reason = "Ctrl+C"

    end = snapshot(cfg)
    mins = (time.monotonic() - t0) / 60
    reqs = end["requests_today"] - start["requests_today"]
    log("=" * 62)
    log(f"停止原因：{stop_reason}")
    log(f"持续时长：{mins:.0f} 分钟（{mins / 60:.1f} 小时），{cycle} 轮")
    log(f"本次会话请求 {reqs} 次"
        f"（{reqs / max(mins / 60, 1e-9):.0f}/小时），今日累计 {end['requests_today']}")
    log(f"code=37 今日 {end['code37_today']} 次")
    log(f"岗位 {start['jobs']} → {end['jobs']}（+{end['jobs'] - start['jobs']}）")
    log(f"有JD {start['with_jd']} → {end['with_jd']}（+{end['with_jd'] - start['with_jd']}）")
    if ladder:
        log("日线抬升轨迹（每一档都是跑满且零封禁才抬的）：")
        for step in ladder:
            log(f"  {step['from']} → {step['to']}"
                f"（当时累计 {step['at_requests']}、code37 {step['code37']}）")
        log(f"★ 最后一个**已验证安全**的日线是 {ladder[-1]['from']}；"
            f"当前档 {day_cap} 的结论看停止原因")
    log(f"心跳明细：{hb_path}")
    log("=" * 62)
    # 抬升轨迹单独存一份，方便下次直接读结论
    if ladder:
        (OUT_DIR / f"ratchet-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
            json.dumps({"ladder": ladder, "final_cap": day_cap,
                        "stop_reason": stop_reason,
                        "requests_today": end["requests_today"],
                        "code37_today": end["code37_today"]},
                       ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
