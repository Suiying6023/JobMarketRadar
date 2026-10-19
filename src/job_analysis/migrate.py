"""数据库迁移：补上决策模型能产出的字段，并从 raw_json 回填平台自带的 jobType。

## 为什么不是"重建新库"

老库有 11,361 条岗位，其中 **100% 的 raw_json 都带 `jobType`**（实测），
意味着平台自己的岗位分类可以**无损回填**，不需要重新抓一遍。
重建新库的代价是重抓 1.1 万条岗位——而重抓正是风控的诱因。

所以选"迁移 + 补采"：结构补列、历史回填、只对新数据重新采集。

## 安全约定

  · 默认 dry-run，必须显式 `--apply` 才写
  · 写之前自动备份到 `data/backups/`，并记录备份文件名
  · 前后各跑一次 `PRAGMA integrity_check`
  · 每一步都打印行数变化，便于对照
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(r"D:\Code\Boss\data\hangzhou_agent.sqlite3")
BACKUP_DIR = Path(r"D:\Code\Boss\data\backups")

# 新列：名字 → SQLite 类型。全部可空，加列不锁表、不重写数据。
NEW_COLUMNS: dict[str, str] = {
    # 平台自带字段，从 raw_json 回填
    "job_type": "INTEGER",
    "daily_rate": "INTEGER",
    # 决策模型产出
    "role_family": "TEXT",
    "role_confidence": "REAL",
    "role_probabilities": "TEXT",
    "title_mismatch": "REAL",
    "seniority_score": "REAL",
    "seniority_confidence": "REAL",
    "employment_type": "TEXT",
    "employment_confidence": "REAL",
    "fresh_grad": "TEXT",
    "fresh_grad_confidence": "REAL",
    # 届别必须单独存：用户是 2026 届，而很多岗位写的是「2027 届」——
    # 把"接受应届"当布尔值会把下一届的岗位也算成可投，这是方向性错误。
    "grad_year": "TEXT",
    "grad_year_confidence": "REAL",
    # 学历也要单独问、单独存：列表的 `education` 字段是**搜索条件的回显**，
    # 不是岗位真实门槛。实测某岗位列表标「本科」、正文写「博士/硕士学位的同学」。
    "education_req": "TEXT",
    "education_confidence": "REAL",
    # 技术栈走规则抽取（不花模型调用），覆盖面比平台自带的 skills 字段广。
    "tech_stack_json": "TEXT",
    "is_proxy": "REAL",
    "relabeled_at": "TEXT",
    "relabel_model": "TEXT",
}

NEW_INDEXES: dict[str, str] = {
    "idx_jobs_job_type": "jobs(job_type)",
    "idx_jobs_role_family": "jobs(role_family)",
    "idx_jobs_salary_min": "jobs(salary_min_k)",
    "idx_jobs_relabeled": "jobs(relabeled_at)",
    "idx_jobs_fresh_grad": "jobs(fresh_grad)",
}


def existing_columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(jobs)")}


def integrity(db: sqlite3.Connection) -> str:
    return db.execute("PRAGMA integrity_check").fetchone()[0]


def backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-{stamp}.sqlite3"
    shutil.copy2(path, target)
    return target


def backfill_platform_fields(db: sqlite3.Connection) -> dict:
    """从 raw_json 回填 jobType 与日薪标记。

    列表原始记录可能在根上，也可能嵌在 list/card/list_job 下——两种都试。
    """
    rows = db.execute("SELECT canonical_id, salary_text, raw_json FROM jobs").fetchall()
    job_types: dict[str, int] = {}
    daily: set[str] = set()
    for canonical_id, salary_text, raw in rows:
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                source = data
                for key in ("list", "card", "list_job"):
                    if isinstance(data.get(key), dict):
                        source = data[key]
                        break
                value = source.get("jobType")
                if isinstance(value, int):
                    job_types[canonical_id] = value
        if "元/天" in (salary_text or ""):
            daily.add(canonical_id)
    return {"job_types": job_types, "daily": daily}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只演练）")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在: {path}")
        return 1

    db = sqlite3.connect(str(path))
    total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"库: {path}  行数: {total}")
    print(f"迁移前完整性: {integrity(db)}")

    columns = existing_columns(db)
    missing = {name: sql for name, sql in NEW_COLUMNS.items() if name not in columns}
    print(f"\n需要新增的列: {len(missing)}")
    for name, sql in missing.items():
        print(f"   + {name:24s} {sql}")

    print("\n=== 回填计划 ===")
    backfill = backfill_platform_fields(db)
    job_types = backfill["job_types"]
    daily = backfill["daily"]
    from collections import Counter
    distribution = Counter(job_types.values())
    print(f"   job_type 可回填: {len(job_types)}/{total}  "
          f"分布={dict(sorted(distribution.items()))}")
    print(f"   日薪岗位: {len(daily)}")

    existing_indexes = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    pending_indexes = {n: s for n, s in NEW_INDEXES.items() if n not in existing_indexes}
    print(f"   待建索引: {list(pending_indexes)}")

    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 执行。")
        db.close()
        return 0

    target = backup(path)
    print(f"\n已备份到: {target}  ({target.stat().st_size / 1024 / 1024:.1f} MB)")

    for name, sql in missing.items():
        db.execute(f'ALTER TABLE jobs ADD COLUMN "{name}" {sql}')
    db.commit()
    print(f"已加列 {len(missing)} 个")

    if job_types:
        db.executemany("UPDATE jobs SET job_type = ? WHERE canonical_id = ?",
                       [(value, key) for key, value in job_types.items()])
        print(f"已回填 job_type {len(job_types)} 行")
    if daily:
        db.executemany("UPDATE jobs SET daily_rate = 1 WHERE canonical_id = ?",
                       [(key,) for key in daily])
        print(f"已回填 daily_rate {len(daily)} 行")
    db.commit()

    for name, target_sql in pending_indexes.items():
        db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target_sql}")
        print(f"已建索引 {name}")
    db.commit()

    after_total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    filled = db.execute("SELECT COUNT(*) FROM jobs WHERE job_type IS NOT NULL").fetchone()[0]
    print(f"\n迁移后行数: {after_total}（应为 {total}）")
    print(f"job_type 已填充: {filled}")
    print(f"迁移后完整性: {integrity(db)}")
    db.execute("PRAGMA optimize")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
