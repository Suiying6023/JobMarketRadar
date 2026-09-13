import asyncio
import os
from datetime import datetime, timezone

import pytest

from job_radar.annotations import annotation_stale, ensure_schema, latest_annotations, record_status, save_annotation
from job_radar.safety import AccessBlocked, AccessGate
from job_radar.service_jobs import finish_request, selected_rows
from job_radar.task_store import TaskOptions
from job_radar.tasks import TaskManager
from job_radar.worker import TaskContext, run
from job_radar.storage import JobStore
from tests.test_managed_service import workspace


def test_task_resume_cannot_release_an_independent_manual_pause(workspace):
    settings, database = workspace
    cfg, rev = settings.read()
    cfg.network_enabled = True
    settings.save(cfg.model_dump(mode="json"), rev)
    gate = AccessGate(database)
    gate.pause("独立人工暂停")
    with TaskManager(settings) as manager:
        task = manager.store.create("collect", TaskOptions(), rev, "browser")
        manager.store.update(task["id"], status="waiting_verification")
        with pytest.raises(AccessBlocked):
            manager.resume(task["id"], True)
        assert gate.read_state().reason == "独立人工暂停"
        state = gate.pause("较晚的任务风险", source=f"managed-task:{task['id']}")
        assert state.source == "manual" and state.reason == "独立人工暂停"


def test_active_tasks_remain_visible_beyond_a_hundred_finished_tasks(workspace):
    settings, _ = workspace
    with TaskManager(settings) as manager:
        active = manager.store.create("collect", TaskOptions(), "rev", "browser")
        for _ in range(105):
            task = manager.store.create("tag", TaskOptions(dry_run=True), "rev", "preview")
            manager.store.finish(task["id"], "succeeded")
        listed = manager.list()
        assert listed["active_count"] == 1
        assert listed["items"][0]["id"] == active["id"]
        assert manager.store.active()[0]["id"] == active["id"]


def test_connection_changes_are_pending_while_batch_fields_apply(workspace):
    settings, _ = workspace
    with TaskManager(settings) as manager:
        cfg, revision = settings.read()
        task = manager.store.create("tag", TaskOptions(), revision, "llm")
        ctx = TaskContext(settings.path, manager.store, task["id"])
        cfg.browser.proxy_server = "http://127.0.0.1:19000"
        cfg, revision2 = settings.save(cfg.model_dump(mode="json"), revision)
        assert ctx.config().browser.proxy_server == ""
        assert manager.store.get(task["id"])["applied_revision"] == revision
        cfg.llm.system_prompt = "新的标注提示词"
        settings.save(cfg.model_dump(mode="json"), revision2)
        effective = ctx.config()
        assert effective.llm.system_prompt == "新的标注提示词"
        assert effective.browser.proxy_server == ""
        assert ctx.revision.startswith("partial:")
        assert "browser.proxy_server" in ctx.progress["pending_configuration"]["fields"]


def test_local_budget_exception_never_creates_a_platform_hold(workspace):
    from job_radar.collectors.boss import RiskControlStop
    settings, database = workspace
    gate = AccessGate(database)
    gate.release("合成测试")
    with TaskManager(settings) as manager:
        task = manager.store.create("collect", TaskOptions(), "rev", "browser")
        ctx = TaskContext(settings.path, manager.store, task["id"])
        async def budget():
            raise RiskControlStop("本轮本地预算已用完")
        with pytest.raises(RiskControlStop):
            asyncio.run(ctx.verified(budget, None, None))
        assert gate.read_state().ready


def test_stop_during_verification_runs_normal_cleanup(workspace, monkeypatch):
    from job_radar.collectors.boss import PlatformRiskStop
    import job_radar.service_jobs
    settings, database = workspace
    gate = AccessGate(database)
    gate.release("合成测试")
    cleaned = []
    with TaskManager(settings) as manager:
        task = manager.store.create("collect", TaskOptions(), "rev", "browser")
        ctx = TaskContext(settings.path, manager.store, task["id"])
        async def operation():
            raise PlatformRiskStop("需要人工验证")
        async def execute(context):
            try:
                await context.verified(operation, None, None)
            finally:
                cleaned.append(True)
        monkeypatch.setattr(job_radar.service_jobs, "execute", execute)
        async def scenario():
            current = asyncio.create_task(run(ctx, os.getppid()))
            for _ in range(30):
                if manager.store.get(task["id"])["status"] == "waiting_verification":
                    break
                await asyncio.sleep(0.05)
            manager.store.command(task["id"], "stop")
            await asyncio.wait_for(current, 2)
        asyncio.run(scenario())
        assert cleaned == [True]
        assert manager.store.get(task["id"])["status"] == "cancelled"


def test_first_status_refresh_is_independent_of_jd_timestamp(workspace):
    settings, database = workspace
    with TaskManager(settings) as manager, JobStore(database) as source:
        ensure_schema(source.conn)
        source.conn.execute("UPDATE jobs SET detail_fetched_at=?", (datetime.now(timezone.utc).isoformat(),))
        source.conn.commit()
        task = manager.store.create("refresh", TaskOptions(), "rev", "browser")
        ctx = TaskContext(settings.path, manager.store, task["id"])
        rows = selected_rows(ctx, source, "refresh", ctx.initial)
        assert len(rows) == 3
        record_status(source.conn, rows[0], task["id"], {"job_status":"招聘中"}, False)
        assert len(selected_rows(ctx, source, "refresh", ctx.initial)) == 2
        record_status(source.conn, rows[1], task["id"], {"job_status":"未知页面"}, None)
        assert len(selected_rows(ctx, source, "refresh", ctx.initial)) == 2


def test_annotation_versions_detect_changed_source_and_prompt(workspace):
    settings, database = workspace
    cfg, rev = settings.read()
    with JobStore(database) as source:
        ensure_schema(source.conn)
        row = dict(source.conn.execute("SELECT * FROM jobs LIMIT 1").fetchone())
        save_annotation(source.conn, row, cfg, rev, "test", {"skills":["Python"]}, {})
        result = latest_annotations(source.conn)[row["canonical_id"]]
        assert not annotation_stale(row, cfg, result)
        assert annotation_stale({**row, "job_description":"新的原文"}, cfg, result)
        cfg.llm.system_prompt += "补充要求"
        assert annotation_stale(row, cfg, result)


def test_refresh_stops_when_ledger_outcome_is_not_persisted():
    class BrokenLedger:
        def mark_outcome(self, *_args):
            return False
    with pytest.raises(RuntimeError, match="账本回填失败"):
        finish_request(BrokenLedger(), 1, "ok")


def test_request_pacing_honors_interval_burst_and_detail_batch(workspace, monkeypatch):
    from job_radar.pacing import RequestPacer
    settings, _ = workspace
    cfg, _ = settings.read()
    cfg.detail_delay_seconds = (2, 2)
    cfg.detail_batch = 2
    cfg.detail_cooldown_seconds = (3, 3)
    cfg.burst_threshold = 2
    cfg.burst_penalty_seconds = (4, 4)
    waits = []
    async def wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr('job_radar.pacing.asyncio.sleep', wait)
    async def scenario():
        pacer = RequestPacer()
        for _ in range(3):
            await pacer.before("detail", cfg)
    asyncio.run(scenario())
    assert waits == [2, 9]
