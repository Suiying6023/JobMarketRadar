"""用决策模型批量重标岗位，带断点续跑。

## 为什么需要断点续跑

7,417 条 × 2.3 秒 ÷ 14 并发 ≈ 20 分钟——听着不长，但无人值守跑两小时，
中途断了要从头再来是不可接受的。所以：
  · 每批提交后立刻写库（不是最后统一写）
  · `relabeled_at` 非空即视为已完成，重启自动跳过
  · 另写一份 JSONL 明细，便于事后复核单条判定

## 失败处理

单条失败只记 `relabel_error`，不中断整批。但**连续失败率过高**会主动停——
那通常意味着 key 失效或上游挂了，继续打只是浪费。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from .decision import DecisionClient, DecisionError, run_batch
from .schema import QUESTIONS, build_state, flatten
from .tech import extract as extract_tech
from .tech import to_json as tech_json

DB = Path(r"D:\Code\Boss\data\hangzhou_agent.sqlite3")
DETAIL_LOG = Path(r"D:\Code\Boss\data\relabel-log.jsonl")
MODEL = "decision-model-preview"

COLUMNS = ("role_family", "role_confidence", "role_probabilities", "title_mismatch",
           "seniority_score", "seniority_confidence", "employment_type",
           "employment_confidence", "fresh_grad", "fresh_grad_confidence",
           "grad_year", "grad_year_confidence", "is_proxy", "tech_stack_json")


def load_rows(db: sqlite3.Connection, limit: int, max_age_days: int | None = 7) -> list[dict]:
    """选出待打标的岗位。

    `max_age_days` 是**额度保护**：默认只打最近 7 天首次见到的岗位。
    老岗位重标一次要花真金白银，而它们大多已经过期、再投也没意义 ——
    要动存量必须显式传 `--max-age-days 0`（0/None = 不限）。

    判据用 `last_seen_at` 而不是 `first_seen_at`：一条三个月前首见、但今天
    又出现在搜索结果里的岗位，仍然是**活的**，值得打标；反过来只看首见时间
    会把它们全漏掉。
    """
    sql = ("SELECT canonical_id, title, company_name, salary_text, experience, education, "
           "job_description FROM jobs "
           "WHERE job_description IS NOT NULL AND TRIM(job_description) != '' "
           "AND (relabeled_at IS NULL OR relabeled_at = '')")
    params: list[str] = []
    if max_age_days:
        # last_seen_at 存的是 UTC（`...Z`），这里用 SQLite 的 julianday 比较，
        # 不依赖 Python 侧时区换算。
        sql += " AND julianday(last_seen_at) >= julianday('now') - ?"
        params.append(str(float(max_age_days)))
    sql += " ORDER BY last_seen_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(sql, params)]


def count_by_age(db: sqlite3.Connection) -> dict[str, int]:
    """统计未打标的岗位里，新旧各有多少 —— 让操作者看清 `--max-age-days` 会跳过什么。"""
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT "
        " SUM(CASE WHEN julianday(last_seen_at) >= julianday('now') - 7 THEN 1 ELSE 0 END) AS recent,"
        " SUM(CASE WHEN julianday(last_seen_at) <  julianday('now') - 7 THEN 1 ELSE 0 END) AS old,"
        " COUNT(*) AS total FROM jobs "
        "WHERE job_description IS NOT NULL AND TRIM(job_description) != '' "
        "AND (relabeled_at IS NULL OR relabeled_at = '')").fetchone()
    return {"recent": row["recent"] or 0, "old": row["old"] or 0, "total": row["total"] or 0}


def write_batch(db: sqlite3.Connection, results: list[tuple[dict, dict, str | None]],
                model: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    updates = []
    for item, answers, error in results:
        if answers is None:
            db.execute(
                "UPDATE jobs SET relabeled_at = ?, relabel_model = ?, "
                "role_family = COALESCE(role_family, 'error') WHERE canonical_id = ?",
                (stamp, f"{model}:failed", item["canonical_id"]))
            continue
        flat = flatten(answers)
        updates.append((
            flat["role_family"], flat["role_confidence"],
            json.dumps(flat["role_probabilities"], ensure_ascii=False),
            flat["title_mismatch"], flat["seniority_score"], flat["seniority_confidence"],
            flat["employment_type"], flat["employment_confidence"],
            flat["fresh_grad"], flat["fresh_grad_confidence"],
            flat["grad_year"], flat["grad_year_confidence"], flat["is_proxy"],
            # 技术栈是**纯规则抽取**（词表匹配），顺手在这里算掉，零额外成本。
            # 之前 `tech.py` 是个没人调用的孤儿库，`tech_stack_json` 靠已删除的
            # 旧脚本维护 —— 存量 7,417 条有值、新岗位全空。
            tech_json(extract_tech(item.get("job_description") or "")),
            stamp, model, item["canonical_id"]))
    if updates:
        placeholders = ", ".join(f"{name} = ?" for name in COLUMNS)
        db.executemany(
            f"UPDATE jobs SET {placeholders}, relabeled_at = ?, relabel_model = ? "
            f"WHERE canonical_id = ?", updates)
    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="用决策模型重标岗位")
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--batch", type=int, default=200, help="每多少条提交一次")
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--dry-run", action="store_true", help="只跑不写库")
    parser.add_argument("--max-age-days", type=float, default=7.0,
                        help="只打最近 N 天内还活跃的岗位（默认 7；传 0 表示不限，"
                             "会连旧存量一起打 —— 费额度，谨慎）")
    parser.add_argument("--stats-only", action="store_true",
                        help="只统计待打标的新旧分布，不调模型")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(r"D:\Code\Boss\src")))

    db = sqlite3.connect(args.db)
    stats = count_by_age(db)
    window = args.max_age_days or None
    print(f"未打标合计 {stats['total']} 条：近 7 天活跃 {stats['recent']}，"
          f"更早 {stats['old']}")
    if window:
        print(f"本次窗口 --max-age-days {window:g} → 预计处理 {stats['recent']} 条"
              f"（跳过 {stats['old']} 条旧数据；要打旧的用 --max-age-days 0）")
    else:
        print(f"⚠ 不限时效 → 将处理全部 {stats['total']} 条（费额度）")
    if args.stats_only:
        return 0

    from job_radar.cli import load_config
    config = load_config(str(Path(r"D:\Code\Boss\config.local.yaml")))
    client = DecisionClient(base_url=config.llm.base_url, api_key=config.llm.api_key,
                            model=MODEL)

    rows = load_rows(db, args.limit, max_age_days=window)
    total = len(rows)
    print(f"待重标: {total} 条  并发: {args.workers}  模型: {MODEL}")
    if not total:
        print("没有待处理数据")
        return 0

    started = time.monotonic()
    done = 0
    ok = 0
    failed = 0
    buffer: list[tuple[dict, dict, str | None]] = []
    consecutive_failures = 0
    log = DETAIL_LOG.open("a", encoding="utf-8")

    try:
        for item, answers, error in run_batch(client, rows, workers=args.workers,
                                              state_of=build_state, questions=QUESTIONS):
            buffer.append((item, answers, error))
            done += 1
            if answers is not None:
                ok += 1
                consecutive_failures = 0
            else:
                failed += 1
                consecutive_failures += 1
            log.write(json.dumps({"canonical_id": item["canonical_id"],
                                  "title": item.get("title"),
                                  "answers": answers, "error": error},
                                 ensure_ascii=False) + "\n")

            if len(buffer) >= args.batch:
                if not args.dry_run:
                    write_batch(db, buffer, MODEL)
                buffer.clear()
                elapsed = time.monotonic() - started
                rate = done / elapsed * 60 if elapsed else 0
                remaining = (total - done) / (done / elapsed) if done and elapsed else 0
                print(f"  [{done}/{total}] ok={ok} failed={failed} "
                      f"{rate:.1f} 条/分  剩余约 {remaining / 60:.1f} 分钟", flush=True)

            # 连续失败太多说明上游有问题，主动停，别继续烧
            if consecutive_failures >= 40:
                print(f"连续失败 {consecutive_failures} 条，主动停止", flush=True)
                break

        if buffer and not args.dry_run:
            write_batch(db, buffer, MODEL)
        buffer.clear()
    finally:
        log.close()
        db.close()

    elapsed = time.monotonic() - started
    print(f"\n完成 {done}/{total}：成功 {ok}，失败 {failed}，"
          f"耗时 {elapsed / 60:.1f} 分钟，{done / elapsed:.2f} 条/秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
