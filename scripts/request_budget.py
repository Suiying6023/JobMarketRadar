"""查请求账本余量：今日已用 / 上限 / 还能抓多少条详情。

`SELECT kind,COUNT(*) ... GROUP BY 1` 只给"已用"，余量还得自己减、还得把
"请求数 → 能抓几条详情"换算一遍（抓 N 条详情 ≈ 1.2N 次请求，因为每约 5 条
会触发一次 code=37 重算，重算本身也是请求）。手算容易错，直接给结论。

用法:
    python scripts/request_budget.py              # 今日
    python scripts/request_budget.py --day 2026-08-10
    python scripts/request_budget.py --history 7  # 近 7 天趋势
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

# 单条详情的账面开销：真源是 `JobStore.REQ_PER_DETAIL`，别在这里另写一份。
REQ_PER_DETAIL = JobStore.REQ_PER_DETAIL


def main() -> None:
    parser = argparse.ArgumentParser()
    # 默认跟当前日常档一致。原来写死 hangzhou（日线 600），而实际在跑
    # steady/cities3（1000/1800）——于是这个脚本会在跑得正常时报"已达日线、余量 0"，
    # 是**假警报**。跑哪个配置就用 --config 传哪个。
    parser.add_argument("--config", default="config.steady.yaml")
    parser.add_argument("--day", default=None, help="默认今天（本地日）")
    parser.add_argument("--history", type=int, default=0, help="额外显示近 N 天")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cap = cfg.max_requests_per_day
    day = args.day or datetime.now().astimezone().strftime("%Y-%m-%d")

    with JobStore(cfg.database_path) as store:
        counts = store.request_counts(day)
        used = counts.get("total", 0)
        left = max(0, cap - used)

        print(f"日期 {day}（本地）")
        print(f"采集链路（带账号 cookie / 家宽直连）")
        print(f"{'':2}已用 {used} / 上限 {cap}  →  余量 {left}")
        if counts:
            for kind, n in sorted(counts.items()):
                if kind != "total" and kind in JobStore.COLLECT_KINDS:
                    print(f"{'':4}{kind:16s} {n}")

        # 刷新链路是另一条独立日线（免登录 + 波尔出口），不占采集额度。
        # 单列出来，免得看账本时把两条线加在一起自己吓自己。
        other = {k: n for k, n in counts.items()
                 if k != "total" and k not in JobStore.COLLECT_KINDS}
        if other:
            print(f"状态刷新链路（免登录 / 波尔出口，独立日线，不占上方额度）")
            for kind, n in sorted(other.items()):
                print(f"{'':4}{kind:16s} {n}")

        # 风控事件（真封禁 vs 可恢复，混在一起看会误判严重度）
        rows = store.conn.execute(
            "SELECT outcome, code, COUNT(*) n FROM request_log "
            "WHERE day = ? AND outcome != 'ok' GROUP BY 1, 2", (day,)
        ).fetchall()
        if rows:
            print(f"{'':2}非 ok 事件:")
            for r in rows:
                tag = "可恢复" if r["outcome"] == "code37_recover" else "⚠️ 需停手"
                print(f"{'':4}{r['outcome']:18s} code={r['code']} × {r['n']}  {tag}")

        print(f"{'':2}还可抓约 {int(left / REQ_PER_DETAIL)} 条详情"
              f"（按 1 条 ≈ {REQ_PER_DETAIL} 次请求）")
        if left == 0:
            print("  ⛔ 已达日线，采集脚本会在请求发出前自行停止")
        elif left < cap * 0.2:
            print("  ⚠️ 余量不足 20%，建议收尾")

        if args.history:
            print(f"\n近 {args.history} 天：")
            hist = store.conn.execute(
                "SELECT day, COUNT(*) n, "
                "SUM(outcome = 'code37_recover') c37, "
                "SUM(outcome IN ('risk', 'throttled')) bad "
                "FROM request_log GROUP BY day ORDER BY day DESC LIMIT ?",
                (args.history,),
            ).fetchall()
            for r in hist:
                warn = "  ⚠️" if r["bad"] else ""
                print(f"{'':2}{r['day']}  {r['n']:4d} 次  code37 {r['c37']}"
                      f"  风控 {r['bad']}{warn}")
        # 账本从 2026-08-10 才有，之前的采集量无记录
        first = store.conn.execute("SELECT MIN(day) d FROM request_log").fetchone()
        if first and first["d"]:
            print(f"\n（账本起始 {first['d']}，更早的采集量无记录）")


if __name__ == "__main__":
    main()
