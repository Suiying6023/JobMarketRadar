"""从请求账本分析配额曲线：code=37 密度是否随当日累计量上升。

**这是判断日线安全性的直接证据**。`max_requests_per_day=600` 目前是按
「08-03 封禁前量级往下取」的保守猜测，不是实测红线。如果 code=37 密度随累计量
明显上升，说明信任度在当日内被消耗，接近某个阈值时会升级到 code=32 封禁——
那 600 就偏高。如果密度平稳，说明配额窗口是无记忆的滑动窗口，600 有余量。

用法:
    python scripts/analyze_quota_curve.py
    python scripts/analyze_quota_curve.py --bucket 25   # 每 25 次请求一档
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--day", default=None)
    parser.add_argument("--bucket", type=int, default=25,
                        help="每多少次请求分一档统计密度")
    args = parser.parse_args()

    cfg = load_config(args.config)
    day = args.day or datetime.now().astimezone().strftime("%Y-%m-%d")

    with JobStore(cfg.database_path) as store:
        rows = store.conn.execute(
            "SELECT id, ts, kind, outcome, code, script FROM request_log "
            "WHERE day = ? ORDER BY id", (day,)
        ).fetchall()
        if not rows:
            print(f"{day} 无账本记录")
            return

        # security_check 行是"重算动作"本身，不是被计数的业务请求；
        # 判 code=37 密度要看它出现在第几次**业务请求**上。
        biz = [r for r in rows if r["kind"] != "security_check"]
        recovers = [r for r in rows if r["outcome"] == "code37_recover"]
        bad = [r for r in rows if r["outcome"] in ("risk", "throttled")]

        print(f"=== {day} 配额曲线（业务请求 {len(biz)} 次，"
              f"code=37 {len(recovers)} 次）===\n")

        # 把每次 code=37 映射到它之前的业务请求序号
        biz_ids = [r["id"] for r in biz]
        marks: list[int] = []
        for rec in recovers:
            n = sum(1 for i in biz_ids if i < rec["id"])
            marks.append(n)

        # 分档密度
        print(f"{'累计业务请求区间':<20} {'请求数':>6} {'code=37':>8} {'密度':>8}")
        print("-" * 46)
        total = len(biz)
        b = args.bucket
        for start in range(0, total, b):
            end = min(start + b, total)
            hits = sum(1 for m in marks if start < m <= end)
            n = end - start
            dens = f"1/{n / hits:.1f}" if hits else "—"
            bar = "█" * hits
            print(f"{start + 1:>4}–{end:<15} {n:>6} {hits:>8} {dens:>8}  {bar}")

        if len(marks) >= 2:
            gaps = [marks[i] - marks[i - 1] for i in range(1, len(marks))]
            print(f"\ncode=37 间距: {gaps}")
            print(f"平均 {sum(gaps) / len(gaps):.1f} 次业务请求")
            half = len(gaps) // 2
            if half >= 2:
                early = sum(gaps[:half]) / half
                late = sum(gaps[half:]) / len(gaps[half:])
                print(f"前半段平均 {early:.1f} / 后半段平均 {late:.1f}")
                if late < early * 0.7:
                    print("  ⚠️ 后半段明显加密 → 信任度在当日内被消耗，日线应下调")
                elif late > early * 1.4:
                    print("  ℹ️ 后半段变稀 → 可能是节流生效或平台放宽")
                else:
                    print("  ✅ 密度平稳 → 配额窗口无记忆，当前日线有余量")

        # 按脚本拆（不同脚本节奏不同，混在一起看会误判）
        print("\n=== 按脚本 ===")
        per = store.conn.execute(
            "SELECT script, kind, COUNT(*) n, "
            "SUM(outcome = 'code37_recover') c37 "
            "FROM request_log WHERE day = ? GROUP BY 1, 2 ORDER BY MIN(id)", (day,)
        ).fetchall()
        for r in per:
            c37 = f"  code37 {r['c37']}" if r["c37"] else ""
            print(f"  {r['script']:20s} {r['kind']:15s} {r['n']:4d} 次{c37}")

        if bad:
            print(f"\n⚠️ 真风控事件 {len(bad)} 次（code=32/36 或节流）：")
            for r in bad:
                print(f"  {r['ts']} {r['script']} {r['outcome']} code={r['code']}")
        else:
            print("\n✅ 零真风控事件（无 code=32/36、无节流文案）")

        used = len(rows)
        print(f"\n今日已用 {used}/{cfg.max_requests_per_day}，"
              f"余量 {max(0, cfg.max_requests_per_day - used)}")


if __name__ == "__main__":
    main()
