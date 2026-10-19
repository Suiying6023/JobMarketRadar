import hashlib
import time

from tests._legacy.test_workbench_http import call, local_server


def test_service_http_preview_options_events_and_invalid_requests(local_server):
    server, database = local_server
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    state = call(server, "/api/state")[2]
    headers = {"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]}
    catalog = call(server, "/api/options")[2]
    assert any(city["name"] == "杭州" for city in catalog["cities"])
    # 任务种类以 task_store.KINDS 为准；recommend（推荐流采集）是后来加的，
    # 这里原先只写了四种，导致目录接口一加新种类测试就红。
    assert {item["value"] for item in catalog["task_kinds"]} == {
        "collect", "recommend", "details", "refresh", "tag"
    }
    assert call(server, "/api/tasks", {"kind": "collect"}, headers)[0] == 409
    assert call(server, "/api/tasks", {"kind": "shell"}, headers)[0] == 400
    assert call(server, "/api/tasks", {"kind": "tag", "options": {"limit": 0}}, headers)[0] == 422
    status, _, task = call(server, "/api/tasks", {"kind": "collect", "options": {"dry_run": True}}, headers)
    assert status == 202
    deadline = time.monotonic()+8
    while task["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
        task = call(server, "/api/tasks/"+task["id"])[2]
    assert task["status"] == "succeeded", task
    events = call(server, f"/api/tasks/{task['id']}/events")[2]
    assert events["items"]
    assert call(server, f"/api/tasks/{task['id']}/events?limit=9999")[0] == 400
    assert call(server, "/api/tasks/nonexistent")[0] == 404
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_config_api_masks_credentials_and_requires_release_confirmation(local_server):
    server, _ = local_server
    state = call(server, "/api/state")[2]
    headers = {"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]}
    state["config"]["llm"]["api_key"] = "synthetic-ui-key-value"
    status, _, saved = call(server, "/api/config", {"config": state["config"], "revision": state["revision"]}, headers)
    assert status == 200 and saved["config"]["llm"]["api_key"] == ""
    assert saved["secrets"]["llm.api_key"]
    assert call(server, "/api/access", {"action": "release"}, headers)[0] == 400
    assert call(server, "/api/access", {"action": "release", "confirmed": True, "confirmation": "本地替身确认"}, headers)[0] == 200
    from job_radar.safety import AccessGate
    assert AccessGate(server.workbench.database).read_state().reason.endswith("本地替身确认")
