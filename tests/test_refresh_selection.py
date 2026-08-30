"""刷新链路**选行**的回归测试：钉死"续跑不重复劳动"。

背景（2026-08-31 实测踩到）：选行按 `detail_fetched_at` 排序，而这一列
**不是刷新脚本写的** —— 刷完一条它在队列里的位置分毫不动。手工按批跑时人记得
跑到哪了，看不出问题；但改成无人值守自动续跑之后，每次重起都从同一批队头开始：
当时队头 200 条里有 153 条是 6 小时内刚刷过的，等于 76% 的配额买重复结论。

判据取 `updated_at`（刷新脚本唯一真正写的时间戳）。这里用真实 SQLite 建表跑
真实 SQL，而不是断言字符串里出现了某个片段 —— 后者在拼错括号时照样通过。
"""
from __future__ import annotations

import sqlite3

# 与 refresh_status_loop.main() 里的选行条件保持一致。
# 复制而非 import：那段 SQL 长在 async main 里，抽出来需要改动运行中的脚本；
# 这里的价值是**独立算一遍**并对齐结果，两边同时错的概率远低于共用一份实现。
BASE = ("WHERE retention_status='active' AND job_invalid=0 "
        "AND job_status_desc != '职位已关闭' AND job_description != '' ")
SKIP_FRESH = ("AND (updated_at IS NULL OR updated_at = '' OR "
              "updated_at < datetime('now','-{h} hours')) ")
ORDER = ("ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
         "THEN 0 ELSE 1 END, detail_fetched_at ASC")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE jobs (
        canonical_id TEXT PRIMARY KEY,
        retention_status TEXT NOT NULL DEFAULT 'active',
        job_invalid INTEGER NOT NULL DEFAULT 0,
        job_status_desc TEXT NOT NULL DEFAULT '',
        job_description TEXT NOT NULL DEFAULT '',
        detail_fetched_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT)""")
    return con


def _add(con, cid, *, updated_at=None, detail_fetched_at="", jd="有 JD",
         status="", invalid=0, retention="active") -> None:
    con.execute("INSERT INTO jobs(canonical_id, retention_status, job_invalid, "
                "job_status_desc, job_description, detail_fetched_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, retention, invalid, status, jd, detail_fetched_at, updated_at))


def _select(con, hours: float) -> list[str]:
    sql = "SELECT canonical_id FROM jobs " + BASE
    if hours > 0:
        sql += SKIP_FRESH.format(h=hours)
    return [r["canonical_id"] for r in con.execute(sql + ORDER)]


def test_recently_refreshed_rows_are_skipped():
    """刚刷过的行不该再进本轮 —— 这正是重复烧配额的那一条。"""
    con = _db()
    _add(con, "fresh", updated_at="2026-08-30 18:00:00")   # datetime('now') 附近
    con.execute("UPDATE jobs SET updated_at = datetime('now','-1 hours') "
                "WHERE canonical_id='fresh'")
    _add(con, "stale")
    con.execute("UPDATE jobs SET updated_at = datetime('now','-40 hours') "
                "WHERE canonical_id='stale'")
    got = _select(con, 12.0)
    assert got == ["stale"], f"1 小时前刷过的行仍被选中: {got}"


def test_never_refreshed_rows_are_kept():
    """`updated_at` 为 NULL / 空串的是从没刷过的，必须留下。

    这是 `IS NULL OR = ''` 那两个分支存在的理由：漏掉它们会把全新岗位
    永久排除在刷新之外 —— 而且没有任何报错，只是永远轮不到。
    """
    con = _db()
    _add(con, "null_ts", updated_at=None)
    _add(con, "empty_ts", updated_at="")
    assert set(_select(con, 12.0)) == {"null_ts", "empty_ts"}


def test_skip_disabled_selects_everything():
    """`--skip-fresh-hours 0` 关闭跳过（手工重跑某批时要用）。"""
    con = _db()
    _add(con, "a", updated_at="2026-08-30 18:00:00")
    con.execute("UPDATE jobs SET updated_at = datetime('now') WHERE canonical_id='a'")
    assert _select(con, 0) == ["a"]


def test_ineligible_rows_still_excluded():
    """跳过条件是**追加**的，不能把原有的三个门槛冲掉。"""
    con = _db()
    _add(con, "no_jd", jd="")
    _add(con, "closed", status="职位已关闭")
    _add(con, "invalid", invalid=1)
    _add(con, "sample", retention="sample")
    _add(con, "good")
    assert _select(con, 12.0) == ["good"]


def test_sort_key_is_not_written_by_refresh():
    """钉死这个 bug 的**根因**，而不只是症状。

    排序键 `detail_fetched_at` 必须仍然只由补详情链路写。哪天有人为了"让队列
    前进"给刷新脚本加上 `SET detail_fetched_at=...`，补详情链路的选行
    （`WHERE job_description=''` + 按抓取时间判新鲜度）就会跟着漂 ——
    刷新会伪造出"详情刚抓过"的假象。这个测试让那种改动必须先解释自己。
    """
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "scripts" / "refresh_status_loop.py").read_text(encoding="utf-8")
    # 只看真正的写语句，注释里提到列名是允许的（现在就有一大段解释）。
    writes = [ln.strip() for ln in src.splitlines()
              if "detail_fetched_at" in ln and "=" in ln
              and not ln.strip().startswith("#")
              and ("UPDATE" in ln.upper() or "SET " in ln.upper())]
    assert not writes, f"刷新脚本不应写排序键 detail_fetched_at: {writes}"
