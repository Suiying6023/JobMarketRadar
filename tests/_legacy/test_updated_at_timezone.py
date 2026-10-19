"""`updated_at` 必须只有一个时区口径（UTC）。

**这条钉的是一个真实存在过的 bug**（2026-08-30 发现并迁移）：
`storage.py` 用 SQLite 的 `CURRENT_TIMESTAMP` 写这一列（**UTC**，8048 行），
而两个刷新脚本用 `datetime.now().isoformat()` 写（**本地时间 UTC+8**，61 行）。
同一列两个时区，相差 8 小时。

危害不是"显示不对"，而是**任何按 updated_at 的时间比较都会静默出错**：
本地时间那批看起来永远比 UTC 那批"更新"，于是
「哪些行最近核实过」「这条降级结论还新不新」这类判断会反过来。

项目里已经踩过一次同形态的坑（`glm_tags_updated_at` 用本地时间却硬编码 `Z` 后缀），
当时的教训是「跨字段比时间前先打出实际值看格式」。这次更进一步：
**同一字段的多个写入方也必须统一口径** —— 字段名相同不代表口径相同。

判据用「SQL 里是否出现 `datetime.now()` 风格的 Python 时间戳」而不是查数据库，
因为测试要能在任何数据库状态下跑。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 会写 jobs.updated_at 的脚本。新增写入方时要加进来。
WRITERS = [
    ROOT / "scripts" / "refresh_status_loop.py",
    ROOT / "scripts" / "check_job_status.py",
]


def _update_statements(src: str) -> list[str]:
    """抠出所有含 updated_at 的 UPDATE 语句片段（跨行拼接的字符串也算）。"""
    out = []
    for m in re.finditer(r"UPDATE\s+jobs\s+SET(.{0,600}?)WHERE", src,
                         re.S | re.I):
        if "updated_at" in m.group(1):
            out.append(m.group(1))
    return out


def test_refresh_writers_use_sqlite_current_timestamp():
    """写 updated_at 一律用 CURRENT_TIMESTAMP，不用 Python 的本地时间。"""
    checked = 0
    for path in WRITERS:
        src = path.read_text(encoding="utf-8")
        for stmt in _update_statements(src):
            checked += 1
            assert "CURRENT_TIMESTAMP" in stmt, (
                f"{path.name} 有一处 updated_at 赋值没用 CURRENT_TIMESTAMP：\n{stmt}")
    assert checked >= 3, f"只找到 {checked} 处 updated_at 赋值，正则可能失效了"


def test_no_local_time_isoformat_feeds_updated_at():
    """`datetime.now().isoformat()` 是本地时间，绝不能进 updated_at。

    这正是被迁移掉的那 61 行的来源。留这条测试是因为
    `datetime.now()` 写起来最顺手，很容易在下次改动里被重新引入。
    """
    for path in WRITERS:
        src = path.read_text(encoding="utf-8")
        for stmt in _update_statements(src):
            assert "isoformat" not in stmt, (
                f"{path.name} 的 updated_at 赋值里出现 isoformat（本地时间）：\n{stmt}")
            assert "datetime.now" not in stmt, (
                f"{path.name} 的 updated_at 赋值里出现 datetime.now（本地时间）：\n{stmt}")
