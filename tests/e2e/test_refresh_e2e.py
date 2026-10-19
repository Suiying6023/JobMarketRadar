"""刷新链路的端到端测试。

和采集链路（`test_pipeline_e2e.py`）同一个做法：每个用例走完整链路，
断言**可观测的外部行为**——发了几个请求、库里几行变了、`job_invalid` 变成什么。

链路上的每一段都是真代码：

| 环节 | 测试里是什么 |
|---|---|
| 取值 | `RefreshPage.evaluate` 真按 `FETCH_JOB_JS` 的契约发 HTTP 到本地回放服务 |
| 解析 | `boss_scrape.htmlpage` 真解析回放服务返回的那份 HTML |
| 判定 | `refresh.classify` 纯函数 |
| 编排 | `refresh.run_batch`：节奏、停机、自愈 |
| 选目标 | `refresh_targets.select_targets`：冷却期与断点续跑 |
| 落库 | `annotations.record_status` → `job_status_checks` / `job_invalid` |

替身只替「浏览器」这一层。之前那版刷新测试是把一个 `{"h1": ..., "job_status": ...}`
字典直接喂给 `classify`，验证的是"我传进去的字典能被解析"，请求怎么发出去的、
HTML 怎么取值的、结果怎么落库的，全都不在覆盖范围内——这也是那三个 bug
（不落库、不续跑、自愈永远失败）能一直藏着的原因。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from boss_scrape.refresh import BatchPolicy, refresh_one, run_batch
from job_radar import refresh_targets
from job_radar.annotations import ensure_schema
from job_radar.storage import JobStore
from tests.support.pages import (challenge_page, gone_page, job_page,
                                 verify_page)
from tests.support.replay import BOSS_ORIGIN, RefreshPage, ReplayServer

JOB_PATH = "/job_detail/e2e000.html"
JOB_URL = f"{BOSS_ORIGIN}{JOB_PATH}"
TITLE = "AI Agent 开发工程师"


class _Roller:
    """stoken 滚动的计数替身。**只数次数**，不碰浏览器。"""

    def __init__(self) -> None:
        self.roll_count = 0
        self.fail_count = 0

    async def roll(self) -> None:
        self.roll_count += 1


@pytest.fixture
def server():
    s = ReplayServer()
    s.route(JOB_PATH, job_page())
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite3") as opened:
        ensure_schema(opened.conn)
        yield opened


def add_job(store, job_id: str = "e2e000", *, title: str = TITLE,
            retention: str = "active") -> dict:
    """插一条岗位，返回 `run_batch` 吃的那份目标。"""
    store.conn.execute(
        "INSERT INTO jobs(canonical_id, platform, source_job_id, job_url, title,"
        " collected_at, first_seen_at, last_seen_at, retention_status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, "boss", job_id, JOB_URL, title, "2026-10-01T00:00:00+00:00",
         "2026-10-01T00:00:00+00:00", "2026-10-18T00:00:00+00:00", retention))
    store.conn.commit()
    return {"canonical_id": job_id, "title": title, "job_url": JOB_URL}


def fast_policy(**overrides) -> BatchPolicy:
    """关掉一切挂钟等待，只留判定与停机逻辑。

    节奏与自愈等待是**运行参数**，不是被测行为；让它们真睡只会把套件从几秒拖到
    一分半（实测：一个用例按 75 秒的默认自愈超时跑完）。
    """
    base = dict(pace=(0.0, 0.0), pause_every=0,
                recover_timeout_s=0.2, recover_poll_s=0.02)
    base.update(overrides)
    return BatchPolicy(**base)


def drain(server: ReplayServer, store, targets, *, roller=None,
          policy: BatchPolicy | None = None, page: RefreshPage | None = None):
    """跑一批，返回 (结果列表, 摘要)。结果在 `on_result` 里就地落库。"""
    page = page or RefreshPage(server)
    results: list[dict] = []

    def on_result(item: dict) -> None:
        results.append(item)
        refresh_targets.record_result(store.conn, item)

    summary = asyncio.run(run_batch(
        page, targets, roller=roller,
        policy=policy or fast_policy(),
        on_result=on_result))
    return results, summary


def checks(store) -> list[tuple]:
    return [tuple(row) for row in store.conn.execute(
        "SELECT canonical_id, outcome FROM job_status_checks ORDER BY id").fetchall()]


def invalid_of(store, job_id: str = "e2e000") -> int:
    return store.conn.execute(
        "SELECT job_invalid FROM jobs WHERE canonical_id = ?", (job_id,)).fetchone()[0]


# ---------------------------------------------------------------- 正常判定与落库


def test_open_job_is_recorded_as_active_with_one_request(server, store):
    """一条在招岗位：1 个请求、1 条检查记录、`job_invalid` 保持 0。"""
    results, summary = drain(server, store, [add_job(store)])

    assert [item["state"] for item in results] == ["open"]
    assert results[0]["job_status"] == "招聘中"
    assert results[0]["active_time"] == "今日活跃"
    assert len(server.calls) == 1, "一条岗位只该发一个文档请求"
    assert checks(store) == [("e2e000", "open")]
    assert invalid_of(store) == 0
    assert summary.stopped == ""


def test_closed_job_is_downgraded_with_its_evidence(server, store):
    """关闭：`job_invalid` 置 1，`job_status_desc` 记下平台文案，证据可复算。"""
    server.route(JOB_PATH, job_page("职位已关闭"))
    results, _ = drain(server, store, [add_job(store)])

    assert results[0]["state"] == "closed"
    assert invalid_of(store) == 1
    row = store.conn.execute(
        "SELECT j.job_status_desc, c.evidence_json FROM jobs j "
        "JOIN job_status_checks c ON c.canonical_id = j.canonical_id "
        "WHERE j.canonical_id = 'e2e000' ORDER BY c.id DESC").fetchone()
    assert row[0] == "职位已关闭"
    evidence = json.loads(row[1])
    assert evidence["job_status"] == "职位已关闭"
    assert evidence["len"] > 60_000
    # 原始 HTML 不许进库：140KB × 数千条会把库撑爆。
    assert "text" not in evidence
    assert evidence["text_len"] > 60_000


def test_gone_page_is_closed_and_never_retried(server, store):
    """下架页（404 + 页面不存在）：判关闭，且**只发一个请求**。

    这条守的是一个真实故障：把下架页和挑战页混成一类，脚本会对已下架的岗位
    反复重试——重试一万次也变不出内容。
    """
    server.route(JOB_PATH, gone_page(), status=404)
    results, _ = drain(server, store, [add_job(store)])

    assert results[0]["state"] == "gone"
    assert invalid_of(store) == 1
    assert len(server.calls) == 1


def test_real_evidence_satisfies_the_task_ledger_verdict(server, store):
    """任务链路（`service_jobs.status_verdict`）直接吃这份证据：下架→关闭，在招→有效。

    这条守的是一个只在生产链路里才露头的缺口：任务链路按 `evidence["gone"]` 判下架，
    而旧取值脚本**从来没给过这个键**，于是下架岗位一直被记成「判不出来」。
    """
    from job_radar.service_jobs import status_verdict

    page = RefreshPage(server)
    row = add_job(store)

    server.route(JOB_PATH, gone_page(), status=404)
    status = asyncio.run(refresh_one(page, JOB_URL, title=TITLE))
    assert status.state == "gone"
    assert status_verdict(row, status.raw) is True

    server.route(JOB_PATH, job_page())
    status = asyncio.run(refresh_one(page, JOB_URL, title=TITLE))
    assert status.state == "open"
    assert status_verdict(row, status.raw) is False


# ---------------------------------------------------------------- 拦截与停机


def test_js_challenge_stops_the_batch_and_never_downgrades(server, store):
    """JS 挑战页：停止本轮，**不动 job_invalid**，只留一条 unknown 检查。

    挑战页不下发种子，所以也不该滚 token（滚了是白跑一趟）。
    """
    server.route(JOB_PATH, challenge_page())
    roller = _Roller()
    results, summary = drain(server, store, [add_job(store), add_job(store, "e2e001")],
                             roller=roller)

    assert [item["state"] for item in results] == ["challenged"]
    assert "撞验证页" in summary.stopped
    assert checks(store) == [("e2e000", "unknown")]
    assert invalid_of(store) == 0, "读不到不能判人家关闭"
    assert len(server.calls) == 1, "撞墙后不许继续打"
    assert roller.roll_count == 0


def test_verify_page_is_recovered_on_a_fresh_page_then_judged(server, store):
    """验证页 → 开新页面自愈 → 重取一次拿到正常页。

    这条钉的是第三个 bug：原先的恢复复用那个已经跑了几十次 fetch 的页面，
    180 条记录里 `recovered` 全为 False。真实恢复必须是一个**干净页面**。
    """
    calls = {"n": 0}

    def payload(_query):
        calls["n"] += 1
        # 前两次（原页面 fetch + 新页面 goto）都是验证页，之后站点自己放行。
        return verify_page() if calls["n"] <= 2 else job_page()

    server.route(JOB_PATH, payload)
    page = RefreshPage(server, heal_after=1)
    results, _ = drain(server, store, [add_job(store)], page=page)

    assert results[0]["state"] == "open", "自愈后应当判出在招"
    assert results[0]["recovered"] is True
    assert len(page.opened) == 1, "恢复必须开新页面，不能复用忙碌的那个"
    assert invalid_of(store) == 0


def test_recovery_navigation_is_reported_so_it_can_be_accounted(server, store):
    """自愈那次导航是一次**真实请求**，任务链路靠这个钩子把它记进账本。

    不报告就等于有一条请求绕过账本——采集链路那条「账本行数 == 真实请求数」
    的不变量会在刷新这里悄悄失效。
    """
    calls = {"n": 0}

    def payload(_query):
        calls["n"] += 1
        return verify_page() if calls["n"] <= 2 else job_page()

    server.route(JOB_PATH, payload)
    page = RefreshPage(server, heal_after=1)
    hits: list[str] = []

    async def before_recover() -> None:
        hits.append("accounted")

    status = asyncio.run(refresh_one(page, JOB_URL, title=TITLE,
                                     recover_timeout_s=0.2, recover_poll_s=0.02,
                                     before_recover=before_recover))

    assert status.recovered is True
    assert hits == ["accounted"], "自愈导航必须被报告一次，且只报告一次"


def test_unrecovered_verify_page_stops_without_retrying(server, store):
    """新页面也恢复不了：停，且**不再重取**。无人值守时重试只会加压。"""
    server.route(JOB_PATH, verify_page())
    page = RefreshPage(server, heal_after=10_000)  # 永远不"自愈"
    results, summary = drain(server, store, [add_job(store)], page=page)

    assert [item["state"] for item in results] == ["challenged"]
    assert "撞验证页" in summary.stopped
    assert invalid_of(store) == 0
    # 1 次原 fetch + 1 次新页面 goto，之后不再重取。
    assert len(server.calls) == 2


def test_three_unreadable_pages_stop_the_batch(server, store):
    """连续三条判不出来就停——否则页面退化后会一路打下去，白烧配额。"""
    server.route(JOB_PATH, "<html><body>" + "无关内容" * 1_000 + "</body></html>")
    targets = [add_job(store, f"e2e00{i}") for i in range(5)]
    results, summary = drain(server, store, targets)

    assert [item["state"] for item in results] == ["unknown"] * 3
    assert "连续 3 条无法判读" in summary.stopped
    assert len(server.calls) == 3, "停机后不该继续发请求"
    assert invalid_of(store, "e2e003") == 0


def test_identity_mismatch_is_left_unknown_not_closed(server, store):
    """标题对不上：判未判定。把 A 的「已关闭」写到 B 上，降级不可逆。"""
    server.route(JOB_PATH, job_page("职位已关闭", h1="销售代表"))
    results, _ = drain(server, store, [add_job(store)])

    assert results[0]["state"] == "unknown"
    assert "身份不符" in results[0]["reason"]
    assert invalid_of(store) == 0


# ---------------------------------------------------------------- stoken 滚动


def test_token_roll_costs_no_extra_request(server, store):
    """取完就地滚 token：0 个额外请求。这是刷新链路省下来的主要成本。"""
    roller = _Roller()
    results, _ = drain(server, store, [add_job(store)], roller=roller)

    assert results[0]["state"] == "open"
    assert roller.roll_count == 1
    assert len(server.calls) == 1, "滚动不能发请求（重铸才发，滚动不发）"


# ---------------------------------------------------------------- 断点续跑


def test_resume_skips_jobs_checked_within_the_cooldown(server, store):
    """刷过一轮之后，第二轮不该再选中同一批。

    这是第二个 bug 的回归：原先按 `detail_fetched_at ASC` 取前 N 条、从不排除
    已刷过的，于是每次运行都选回同一批，进度永远推不动。
    """
    for index in range(3):
        add_job(store, f"e2e00{index}")

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3)
    assert [item["canonical_id"] for item in picked] == ["e2e000", "e2e001", "e2e002"]

    drain(server, store, [{"canonical_id": "e2e000", "title": TITLE, "job_url": JOB_URL}])

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3)
    assert [item["canonical_id"] for item in picked] == ["e2e001", "e2e002"], \
        "刚查过的要跳过，否则断点续跑永远在原地"

    # 冷却期写 0 = 不做冷却，整条条件去掉（「想全量重刷」的显式开关）。
    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=0)
    assert "e2e000" in [item["canonical_id"] for item in picked]


def test_unknown_check_does_not_start_the_cooldown(store):
    """判不出来的不算查过：下一轮还要再查，否则会永久漏掉这批。"""
    add_job(store, "e2e000")
    refresh_targets.record_result(store.conn, {
        "canonical_id": "e2e000", "title": TITLE, "job_url": JOB_URL,
        "state": "unknown", "invalid": None, "evidence": {"reason": "short"}})

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3)
    assert [item["canonical_id"] for item in picked] == ["e2e000"]


def test_cooldown_reads_timestamps_as_times_not_strings(store):
    """冷却期按**时间**比，不按字符串比。

    两个坑：`isoformat()` 在微秒为 0 时会省掉小数部分（`+` 比 `.` 小，同一秒内会颠倒），
    以及补录进来的历史时间不带时区。用 `julianday()` 比较两种都吃得住。
    """
    from datetime import datetime, timedelta, timezone

    add_job(store, "e2e000")
    add_job(store, "e2e001")
    old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    for job_id, stamp in (("e2e000", old.isoformat()), ("e2e001", recent.isoformat())):
        store.conn.execute(
            "INSERT INTO job_status_checks(canonical_id,task_id,checked_at,outcome,"
            "evidence_json) VALUES (?,?,?,'open','{}')", (job_id, "t", stamp))
    store.conn.commit()

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3)
    assert [item["canonical_id"] for item in picked] == ["e2e000"], \
        "10 天前（不带时区）的该重查，1 小时前的该跳过"


def test_selection_respects_scope_and_exclusions(store):
    """选目标的三道筛子：只刷要投的那批、跳过已判失效的、跳过本轮已处理的。

    最后一条是给双出口并行用的：两个 worker 各选一批，靠 `exclude` 避免撞车。
    """
    add_job(store, "e2e000")
    add_job(store, "e2e001", retention="sample")   # 去重样本，刷了不影响决策
    add_job(store, "e2e002")
    add_job(store, "e2e003")
    store.conn.execute("UPDATE jobs SET job_invalid = 1 WHERE canonical_id = 'e2e003'")
    store.conn.commit()

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3)
    assert [item["canonical_id"] for item in picked] == ["e2e000", "e2e002"]

    picked = refresh_targets.select_targets(store.conn, limit=10, min_age_days=3,
                                            exclude={"e2e000"})
    assert [item["canonical_id"] for item in picked] == ["e2e002"]


# ---------------------------------------------------------------- 补录


def test_replay_backfills_the_ledger_with_observed_timestamps(tmp_path, store):
    """历史 JSONL 按**原观测时间**补录，且同岗位多条只留最后一条。

    这条守的是第一个 bug 的补救路径：已经攒下的记录不该重刷一遍才能入库
    （那是白烧请求）。
    """
    path = tmp_path / "refresh-batch.jsonl"
    rows = [
        {"canonical_id": "e2e000", "title": TITLE, "job_url": JOB_URL,
         "state": "open", "invalid": False, "evidence": {"job_status": "招聘中"},
         "at": "2026-10-18T10:00:00+00:00"},
        {"canonical_id": "e2e000", "title": TITLE, "job_url": JOB_URL,
         "state": "closed", "invalid": True, "evidence": {"job_status": "职位已关闭"},
         "at": "2026-10-18T12:00:00+00:00"},
        {"canonical_id": "e2e001", "title": TITLE, "job_url": JOB_URL,
         "state": "gone", "invalid": True, "evidence": {"reason": "gone"},
         "at": "2026-10-18T11:00:00+00:00"},
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                    encoding="utf-8")
    add_job(store, "e2e000")
    add_job(store, "e2e001")

    summary = refresh_targets.replay(path, store.conn)

    assert summary["jobs"] == 2, "同一岗位的旧结论不该覆盖新结论"
    assert summary["states"] == {"closed": 1, "gone": 1}
    stored = store.conn.execute(
        "SELECT canonical_id, checked_at, outcome FROM job_status_checks "
        "ORDER BY checked_at").fetchall()
    assert [tuple(row) for row in stored] == [
        ("e2e001", "2026-10-18T11:00:00+00:00", "closed"),
        ("e2e000", "2026-10-18T12:00:00+00:00", "closed"),
    ]
    assert invalid_of(store, "e2e000") == 1
    assert invalid_of(store, "e2e001") == 1

    # dry_run 只报数，一行都不写。
    before = len(checks(store))
    dry = refresh_targets.replay(path, store.conn, dry_run=True)
    assert dry["dry_run"] is True and dry["written"] == 2
    assert len(checks(store)) == before
