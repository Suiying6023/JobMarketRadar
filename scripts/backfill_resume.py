"""backfill 断点续跑：遇风控停止后自动等待并重启，直到补完或达上限。

backfill_details.py 的 SQL 条件 job_description='' 天然支持断点——成功写入 JD 的
记录下次自动跳过。本脚本循环调用它：
1. 遇 RiskControlStop 停止 → 等冷却（默认 10 分钟）→ 重启
2. 直到待补数归零或达到最大重启次数
3. 每次重启前先查待补数，为 0 则结束

用法: python scripts/backfill_resume.py [--limit N] [--cooldown S] [--max-restarts N]
"""
from __future__ import annotations

import argparse
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def pending_count(include_sample: bool = False) -> int:
    """待补详情数。必须与 backfill_details.py 的口径一致——两边都要加 retention 过滤，
    否则 backfill_resume 认为「还有 N 条」而 backfill_details 实际选不到任何行，
    导致每次循环都「待补数没下降 → 判全部跳过 → 重试」，空转 20 轮浪费 3 小时。
    （实测：不加过滤时 pending_count()=231，加过滤后=0。）"""
    con = sqlite3.connect(DB)
    retention_clause = "" if include_sample else "AND retention_status = 'active' "
    n = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_description = '' "
        f"AND raw_json != '{{}}' AND job_invalid = 0 {retention_clause}"
    ).fetchone()[0]
    con.close()
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--cooldown", type=int, default=600,
                        help="风控停止后等待秒数（默认 600 = 10 分钟）")
    parser.add_argument("--max-restarts", type=int, default=20)
    args = parser.parse_args()

    restarts = 0
    while True:
        left = pending_count()
        print(f"\n=== 本轮: 待补 {left} 条（已重启 {restarts} 次）===", flush=True)
        if left == 0:
            print("待补数为 0，全部完成")
            return

        cmd = [str(VENV_PY), "-u", str(ROOT / "scripts" / "backfill_details.py"),
               "--config", "config.hangzhou.yaml", "--limit", str(args.limit)]
        result = subprocess.run(cmd, cwd=ROOT)

        if result.returncode == 0:
            # 正常退出：可能是补完了（待补为0）或完成了本批
            left_after = pending_count()
            if left_after == 0:
                print("全部补完")
                return
            # 待补数没下降 = 全部跳过（比如 detail 去重前置命中率 100%），再跑也是空转
            if left_after >= left:
                print(f"待补数未下降（{left}→{left_after}），可能全部跳过，退出")
                return
            print(f"本轮退出码 0，剩余 {left_after} 条，继续")

        restarts += 1
        if restarts > args.max_restarts:
            print(f"达最大重启次数 {args.max_restarts}，退出")
            return

        print(f"等待 {args.cooldown}s 后重启...", flush=True)
        time.sleep(args.cooldown)


if __name__ == "__main__":
    main()
