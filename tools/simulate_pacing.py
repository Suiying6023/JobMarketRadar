"""离线验证 RequestPacer 的实际速率（不联网、不 sleep）。

按 `sweep` 的真实形态模拟：每个查询 1 个列表请求 + N 条详情，
每条详情之后回传**真实的 JD 字数**（从库里采样），让内容停留系数也参与计算。

把 asyncio.sleep 换成累加器，跑够样本后算平均间隔与每小时吞吐。
"""
from __future__ import annotations

import asyncio
import random
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.pacing import RequestPacer  # noqa: E402
from job_radar.throttle import lognormal_pause  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"

accumulated = {"t": 0.0}
_real_sleep = asyncio.sleep


async def fake_sleep(delay, *a, **kw):
    accumulated["t"] += delay


def jd_lengths(n: int) -> list[int]:
    """从库里采真实 JD 字数，模拟"读到的正文有多长"。"""
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "select length(job_description) from jobs "
            "where retention_status='active' and job_invalid=0 and job_description<>'' "
            "order by random() limit ?", (n,)).fetchall()
    finally:
        conn.close()
    values = [r[0] for r in rows if r[0]]
    return values or [800] * n


async def simulate(cfg, queries: int = 8, per_query: int = 8) -> dict:
    asyncio.sleep = fake_sleep
    try:
        accumulated["t"] = 0.0
        pacer = RequestPacer()
        lengths = jd_lengths(queries * per_query)
        marks: list[float] = []
        details = 0
        for q in range(queries):
            await pacer.before("list", cfg)
            marks.append(accumulated["t"])
            for i in range(per_query):
                await pacer.before("detail", cfg)
                marks.append(accumulated["t"])
                details += 1
                pacer.signal("ok")
                pacer.signal("content", lengths[details % len(lengths)])
    finally:
        asyncio.sleep = _real_sleep
    gaps = [marks[i + 1] - marks[i] for i in range(len(marks) - 1)]
    detail_gaps = [g for i, g in enumerate(gaps) if (i % (per_query + 1)) != per_query]
    mean = statistics.mean(detail_gaps)
    return {
        "details": details,
        "total_s": round(accumulated["t"], 1),
        "mean_gap": round(mean, 2),
        "median_gap": round(statistics.median(detail_gaps), 2),
        "p90_gap": round(sorted(detail_gaps)[int(len(detail_gaps) * 0.9)], 1),
        "max_gap": round(max(detail_gaps), 1),
        "per_hour": round(3600 / mean),
    }


def main() -> None:
    cfg = load_config(str(ROOT / "config.local.yaml"))
    print("=== 当前 config.local.yaml ===")
    for k in ("request_delay_seconds", "detail_delay_seconds", "detail_batch",
              "detail_cooldown_seconds", "burst_threshold", "burst_penalty_seconds"):
        print(f"  {k:26} {getattr(cfg, k)}")
    print()

    print("=== lognormal_pause([6,11]) 形状 ===")
    s = sorted(lognormal_pause(6.0, 11.0) for _ in range(30000))
    n = len(s)
    print(f"  均值 {statistics.mean(s):.2f}  中位 {s[n//2]:.2f}  最小 {s[0]:.2f}  最大 {s[-1]:.1f}")
    print(f"  p90 {s[int(n*0.90)]:.1f}  p95 {s[int(n*0.95)]:.1f}  p99 {s[int(n*0.99)]:.1f}")
    print()

    print("=== 端到端模拟（8 查询 × 8 详情/查询，真实 JD 字数）===")
    runs = [asyncio.run(simulate(cfg)) for _ in range(3)]
    for i, r in enumerate(runs, 1):
        print(f"  第{i}轮: {r['mean_gap']}s/条  中位 {r['median_gap']}  p90 {r['p90_gap']}  "
              f"最大 {r['max_gap']}  →  {r['per_hour']} 条/小时")
    rates = [r["per_hour"] for r in runs]
    means = [r["mean_gap"] for r in runs]
    print()
    print(f"  平均 {statistics.mean(means):.2f}s/条 → **{statistics.mean(rates):.0f} 条/小时**"
          f"（区间 {min(rates)}–{max(rates)}）")
    print(f"  目标 150–250 条/小时：{'✓ 在范围内' if 150 <= statistics.mean(rates) <= 250 else '✗ 超出范围'}")


if __name__ == "__main__":
    main()
