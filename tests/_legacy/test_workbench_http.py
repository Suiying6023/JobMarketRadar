from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from job_radar.models import JobRecord, Platform
from job_radar.settings import init_config
from job_radar.storage import JobStore
from job_radar.workbench import Workbench, WorkbenchServer


@pytest.fixture
def local_server(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with JobStore(db) as store:
        store.upsert(JobRecord(platform=Platform.BOSS, source_job_id="example1", title="AI Agent开发", company_name="示例企业",
                               city="杭州", job_url="https://www.zhipin.com/job_detail/example1.html", job_description="开发 Python RAG 应用。"))
    config = init_config(tmp_path / "config.local.yaml", str(db))
    workbench = Workbench(config)
    server = WorkbenchServer(0, workbench)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, db
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(server, path, data=None, headers=None):
    req = Request(f"http://127.0.0.1:{server.server_port}{path}",
                  data=None if data is None else json.dumps(data).encode(), headers=headers or {})
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        return response.status, response.headers, json.loads(body)


def test_rejects_csrf_wrong_hosts_and_simple_posts(local_server):
    server, _ = local_server
    status, headers, state = call(server, "/api/state")
    assert status == 200
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    assert call(server, "/api/state", headers={"Host": "malicious.example"})[0] == 403
    action = {"action": "applied", "value": True}
    assert call(server, "/api/jobs/boss%3Aexample1/action", action, {"Content-Type": "application/json"})[0] == 403
    good = {"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]}
    assert call(server, "/api/jobs/boss%3Aexample1/action", action, {**good, "Origin": "https://malicious.example"})[0] == 403
    assert call(server, "/api/jobs/boss%3Aexample1/action", action, {**good, "Content-Type": "text/plain"})[0] == 415


def test_notes_and_config_survive_without_changing_imported_evidence(local_server):
    server, db = local_server
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    _, _, state = call(server, "/api/state")
    headers = {"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]}
    assert call(server, "/api/jobs/boss%3Amissing/action", {"action": "applied", "value": True}, headers)[0] == 404
    assert call(server, "/api/jobs/boss%3Aexample1/action", {"action": "applied", "value": True}, headers)[0] == 200
    assert call(server, "/api/jobs/boss%3Aexample1")[2]["applied"] is True
    payload = state["config"]
    payload["keywords"] = ["RAG", "Agent"]
    assert call(server, "/api/config", {"config": payload, "revision": state["revision"]}, headers)[0] == 200
    assert call(server, "/api/config", {"config": payload, "revision": state["revision"]}, headers)[0] == 409
    assert call(server, "/api/state")[2]["config"]["keywords"] == ["RAG", "Agent"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chat_actions").fetchone()[0] == 0
        assert conn.execute("SELECT bookmarked FROM jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_only_allowlisted_assets_are_served_and_filters_are_bounded(local_server):
    server, _ = local_server
    assert call(server, "/..%2f.env")[0] == 404
    assert call(server, "/api/jobs?page_size=10000")[0] == 400
    assert call(server, "/api/jobs?tier=unknown")[0] == 400
    assert call(server, "/api/jobs?" + "&".join(f"x{i}=1" for i in range(30)))[0] == 400
