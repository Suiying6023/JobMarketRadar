from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from job_radar.models import JobRecord
from job_radar.safety import AccessGate, AccessBlocked
from job_radar.service_config import public_config
from job_radar.settings import SettingsStore, init_config
from job_radar.storage import JobStore
from job_radar.task_store import TERMINAL, TaskConflict, TaskOptions
from job_radar.tasks import TaskManager
from job_radar.worker import TaskContext


@pytest.fixture
def workspace(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with JobStore(db) as store:
        for index in range(3):
            store.upsert(JobRecord(platform="boss", source_job_id=f"example{index}", title="应用工程师",
                city="成都", company_name="示例企业", job_url=f"https://www.zhipin.com/job_detail/example{index}.html",
                job_description="使用 Python 开发应用。"))
    settings = SettingsStore(init_config(tmp_path / "config.local.yaml", str(db)))
    return settings, db


@pytest.fixture
def model_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.calls.append({"body": body, "authorization": self.headers.get("Authorization")})
            self.server.entered.set()
            if self.server.block_first and len(self.server.calls) == 1:
                self.server.release.wait(8)
            payload = json.dumps({"choices": [{"message": {"content": '{"skills":["Python"],"role":"应用开发"}'}}],
                                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.calls = []
    server.entered = threading.Event()
    server.release = threading.Event()
    server.block_first = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.release.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def enable_model(settings, server):
    cfg, rev = settings.read()
    cfg.llm.enabled = True
    cfg.llm.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    cfg.llm.model = "test-model"
    cfg.llm.api_key = "synthetic-test-secret-only"
    cfg.service.batch_size = 1
    cfg.service.stop_timeout_seconds = 1
    return settings.save(cfg.model_dump(mode="json"), rev)


def wait_task(manager, ident, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.store.get(ident)
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.05)
    pytest.fail(f"Task did not finish: {manager.store.get(ident)}")


def test_preview_uses_real_worker_and_preserves_source(workspace):
    settings, db = workspace
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with TaskManager(settings) as manager:
        with pytest.raises(AccessBlocked):
            manager.start("collect")
        task = manager.start("collect", {"dry_run": True, "limit": 25})
        result = wait_task(manager, task["id"])
        assert result["status"] == "succeeded"
        assert result["result"]["dry_run"] is True
        events = manager.store.events(task["id"])
        assert any(event["type"] == "plan" for event in events["items"])
        assert manager.store.events(task["id"], events["next_cursor"])["items"] == []
        with pytest.raises(TaskConflict):
            TaskManager(settings)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_secret_roundtrip_is_masked_and_clear_is_explicit(workspace, model_server):
    settings, _ = workspace
    cfg, rev = enable_model(settings, model_server)
    public = public_config(cfg)
    assert public["llm"]["api_key"] == ""
    saved, rev = settings.save(public, rev)
    assert saved.llm.api_key == "synthetic-test-secret-only"
    saved, _ = settings.save(public_config(saved), rev, ["llm.api_key"])
    assert saved.llm.api_key == ""
    for history in (settings.path.parent/".config-history").glob("*.yaml"):
        assert "synthetic-test-secret-only" not in history.read_text(encoding="utf-8")


def test_tag_worker_applies_prompt_at_batch_boundary_and_retains_versions(workspace, model_server):
    settings, db = workspace
    model_server.block_first = True
    cfg, rev = enable_model(settings, model_server)
    initial_prompt = cfg.llm.system_prompt
    with TaskManager(settings) as manager:
        task = manager.start("tag", {"limit": 2})
        assert model_server.entered.wait(8)
        with pytest.raises(TaskConflict):
            manager.start("tag")
        cfg.llm.system_prompt = "新的通用标签格式，请输出 JSON 对象。"
        _, new_revision = settings.save(cfg.model_dump(mode="json"), rev)
        model_server.release.set()
        result = wait_task(manager, task["id"])
        assert result["status"] == "succeeded", result
        assert result["progress"]["processed"] == 2
        assert result["applied_revision"] == new_revision
        assert "synthetic-test-secret-only" not in json.dumps(manager.store.events(task["id"]), ensure_ascii=False)
    assert model_server.calls[0]["body"]["messages"][0]["content"] == initial_prompt
    assert model_server.calls[1]["body"]["messages"][0]["content"] == cfg.llm.system_prompt
    assert model_server.calls[0]["authorization"] == "Bearer synthetic-test-secret-only"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(DISTINCT prompt_hash) FROM job_annotations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM annotation_prompts").fetchone()[0] == 2
        assert 'job_description' in connection.execute("SELECT input_json FROM job_annotations LIMIT 1").fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE job_description='使用 Python 开发应用。'").fetchone()[0] == 3


def test_stop_cancels_inflight_model_work_without_a_late_write(workspace, model_server):
    settings, db = workspace
    model_server.block_first = True
    enable_model(settings, model_server)
    with TaskManager(settings) as manager:
        task = manager.start("tag", {"limit": 2})
        assert model_server.entered.wait(8)
        process = manager._workers[task["id"]][0]
        manager.stop(task["id"])
        assert wait_task(manager, task["id"])["status"] == "cancelled"
        manager.close()
        assert process.poll() is not None
    model_server.release.set()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_annotations").fetchone()[0] == 0


def test_verification_waits_for_explicit_confirmation_and_rechecks(workspace):
    from job_radar.collectors.boss import PlatformRiskStop
    settings, db = workspace
    cfg, rev = settings.read()
    cfg.network_enabled = True
    settings.save(cfg.model_dump(mode="json"), rev)
    gate = AccessGate(db)
    gate.release("测试替身")

    class Browser:
        shown = 0
        async def show(self, page):
            self.shown += 1

    class Page:
        url = "https://www.zhipin.com/web/geek/jobs"

    with TaskManager(settings) as manager:
        task = manager.store.create("collect", TaskOptions(), rev, "browser")
        context = TaskContext(settings.path, manager.store, task["id"])
        browser = Browser()
        calls = []

        async def operation():
            context.require_access()
            calls.append(1)
            if len(calls) == 1:
                raise PlatformRiskStop("需要验证")
            return "valid-business-response"

        async def scenario():
            running = asyncio.create_task(context.verified(operation, browser, Page()))
            await asyncio.sleep(0.1)
            assert manager.store.get(task["id"])["status"] == "waiting_verification"
            assert gate.read_state().mode == "paused" and len(calls) == 1
            with pytest.raises(ValueError):
                manager.resume(task["id"], False)
            manager.show_verification(task["id"])
            await asyncio.sleep(0.3)
            assert browser.shown == 1 and len(calls) == 1
            manager.resume(task["id"], True)
            assert await asyncio.wait_for(running, 2) == "valid-business-response"
            assert len(calls) == 2

        asyncio.run(scenario())


def test_refresh_evidence_does_not_change_jd_time(workspace):
    from job_radar.annotations import ensure_schema, record_status
    from job_radar.service_jobs import status_verdict
    _, db = workspace
    with JobStore(db) as store:
        ensure_schema(store.conn)
        row = dict(store.conn.execute("SELECT * FROM jobs LIMIT 1").fetchone())
        evidence = {"status": 200, "url": row["job_url"], "h1": row["title"], "job_status": "职位已关闭"}
        assert status_verdict(row, evidence) is True
        record_status(store.conn, row, "test", evidence, True)
        saved = store.conn.execute("SELECT * FROM jobs WHERE canonical_id=?", (row["canonical_id"],)).fetchone()
        assert saved["job_invalid"] and saved["detail_fetched_at"] == row["detail_fetched_at"]
        assert status_verdict(row, {**evidence, "h1": "另一个岗位"}) is None
        assert status_verdict(row, {**evidence, "url": "https://www.zhipin.com/job_detail/other.html"}) is None


def test_llm_redirect_does_not_forward_credentials_to_another_origin():
    from urllib.request import Request
    from job_radar.llm import LLMError, SameOriginRedirect
    request = Request("https://model.example/v1/chat/completions", headers={"Authorization": "Bearer synthetic"})
    with pytest.raises(LLMError):
        SameOriginRedirect().redirect_request(request, None, 302, "Found", {}, "https://another.example/capture")


def test_unfinished_tasks_become_interrupted_on_service_restart(workspace):
    settings, _ = workspace
    with TaskManager(settings) as first:
        task = first.store.create("collect", TaskOptions(), "test-revision", "browser")
    with TaskManager(settings) as second:
        assert second.store.get(task["id"])["status"] == "interrupted"


def test_city_options_use_cached_platform_codes(workspace):
    from job_radar.options import option_catalog
    settings, _ = workspace
    cfg, _ = settings.read()
    catalog = option_catalog(cfg, [{"city": "成都", "raw_json": '{"city":101270100}'},
                                  {"city": "未知城市", "raw_json": "malformed"}])
    assert {"name": "成都", "boss_code": "101270100"} in catalog["cities"]
    assert not any(city["name"] == "未知城市" for city in catalog["cities"])
