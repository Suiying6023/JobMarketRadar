"""刷新链路的取数与回写 —— 断点续跑与补录都在这里。

## 为什么单独成模块

批量刷新有三个与"判定"无关、但一定会出错的环节：

1. **选谁**：上一版按 `detail_fetched_at ASC` 取前 N 条，却从不排除"已经刷过的"，
   于是每次运行都选中同一批。实测 180 条记录只覆盖了 91 个岗位，进度永远推不动。
2. **记哪**：结果只写进 JSONL、`--apply` 一次都没带过，`job_invalid` 与
   `job_status_checks` 一条都没更新，看起来"刷了"其实库里什么都没变。
3. **补录**：已经攒下的 JSONL 不能重刷一遍才能入库（那是白烧请求），
   要能按原观测时间回填。

三件事都只碰数据库，不碰浏览器，所以放一起、可脱网测试。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .annotations import record_status

DEFAULT_TASK = "refresh-batch"

# 选目标时的取数范围：真正要投的那批。
# `sample` 那 7,645 条是去重样本，刷了也不影响决策。
ACTIVE_SCOPE = "retention_status = 'active' AND job_invalid = 0"

_SELECT = """
SELECT j.canonical_id, j.title, j.job_url,
       (SELECT MAX(c.checked_at) FROM job_status_checks c
         WHERE c.canonical_id = j.canonical_id
           AND c.outcome IN ('open','closed')) AS last_check
  FROM jobs j
 WHERE {scope}
   AND j.job_url IS NOT NULL AND TRIM(j.job_url) != ''
   {cooldown}
 ORDER BY COALESCE(julianday(last_check), 0) ASC,
          COALESCE(julianday(NULLIF(j.detail_fetched_at, '')), 0) ASC
 LIMIT ?
"""

_COOLDOWN = """
   AND NOT EXISTS (
        SELECT 1 FROM job_status_checks c
         WHERE c.canonical_id = j.canonical_id
           AND c.outcome IN ('open','closed')
           AND julianday(c.checked_at) >= julianday(?))
"""

# ⚠️ 时间比较一律走 `julianday()`，**不要直接比 ISO 字符串**。
# 字符串比较看起来能用，但 `isoformat()` 在微秒为 0 时会省掉小数部分
# （`...T07:52:01+00:00` vs `...T07:52:01.123456+00:00`），此时 `+`(0x2B) < `.`(0x2E)，
# 于是"同一秒内"的两条时间会被判成先后颠倒。
# 另外库里还有补录进来的、不带时区的历史时间，字符串口径根本没法比。


def select_targets(conn, *, limit: int, min_age_days: float = 3.0,
                   scope: str = ACTIVE_SCOPE,
                   exclude: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """按"最久没查过"排序取目标，跳过冷却期内的与本次已处理的。

    `min_age_days` 是冷却期：**只有成功的检查（open/closed）才算数**，
    `unknown` 不进 `last_check` —— 判不出来不代表查过了，下一轮还要再查。

    **`min_age_days <= 0` 表示不做冷却**，直接把整条条件去掉，而不是把 cutoff 设成"现在"。
    后者看着等价，实则不等价：Windows 的系统时钟只有约 15.6ms 的粒度，两次
    `datetime.now()` 可能取到**同一个值**，于是"刚写进去的那条"正好落在 cutoff 上，
    `>=` 成立、被当成冷却期内跳过 —— 实测偶发一次（全量跑红、单跑绿）。
    「0 就是全量重刷」这种写法没有这个歧义。

    `exclude` 给"本轮已处理"用：同一进程里先写库再选，正常不会重；但补录、
    双出口并行时两个 worker 会各选一批，靠它避免撞车。
    """
    args: list = []
    if min_age_days > 0:
        args.append((datetime.now(timezone.utc)
                     - timedelta(days=min_age_days)).isoformat())
        cooldown = _COOLDOWN
    else:
        cooldown = ""
    args.append(limit + len(exclude))
    rows = conn.execute(_SELECT.format(scope=scope, cooldown=cooldown), args).fetchall()
    picked: list[dict] = []
    for row in rows:
        item = {"canonical_id": row[0], "title": row[1], "job_url": row[2],
                "last_check": row[3]}
        if item["canonical_id"] in exclude:
            continue
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def record_result(conn, item: dict, *, task_id: str = DEFAULT_TASK,
                  checked_at: str | None = None) -> None:
    """把一条 `run_batch` 的结果写进库。

    `invalid is None`（判不出来）只记一条检查记录，**不动 `job_invalid`** ——
    降级是单向的，不能因为一次读不到就判人家关闭。
    """
    row = {"canonical_id": item["canonical_id"], "title": item.get("title") or "",
           "job_url": item.get("job_url") or ""}
    record_status(conn, row, task_id, item.get("evidence") or {},
                  item.get("invalid"), checked_at=checked_at or item.get("at"))


def read_jsonl(path: str | Path) -> list[dict]:
    """读回放文件。坏行跳过 —— 一个写了一半的进程不该让整批补录失败。"""
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def latest_by_job(rows: list[dict]) -> list[dict]:
    """同一岗位多条记录只留**最后一条**：重复刷出来的旧结论不该覆盖新结论。"""
    ordered: dict[str, dict] = {}
    for item in rows:
        key = item.get("canonical_id")
        if not key:
            continue
        ordered[key] = item
    return list(ordered.values())


def replay(path: str | Path, conn, *, task_id: str = DEFAULT_TASK,
           dry_run: bool = False) -> dict:
    """把历史 JSONL 补录进库，按原观测时间。

    返回一个计数摘要，**不静默**：补录了多少条、跳过了多少条、各状态多少，
    都要能一眼看出来。补录不产生任何平台请求。
    """
    rows = latest_by_job(read_jsonl(path))
    summary = {"file": str(path), "jobs": len(rows), "written": 0,
               "skipped": 0, "states": {}}
    for item in rows:
        state = item.get("state") or "?"
        summary["states"][state] = summary["states"].get(state, 0) + 1
        if not item.get("canonical_id"):
            summary["skipped"] += 1
            continue
        if not dry_run:
            record_result(conn, item, task_id=task_id)
        summary["written"] += 1
    if dry_run:
        summary["dry_run"] = True
    return summary
