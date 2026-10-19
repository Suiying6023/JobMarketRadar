"""One bounded maintenance cycle, invoked by the external scheduler."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import ServiceClient
from .local_files import atomic_write, file_mutex
from .settings import SettingsStore, resolve_database
from .service_config import public_config, redact
from .storage import JobStore
from .task_store import ACTIVE


class MaintenancePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$")
    config: str
    output: str
    ends_at: datetime
    service_url: str = "http://127.0.0.1:8765"
    keyword_groups: list[list[str]] = Field(min_length=1)
    collect_limit: int = Field(default=45, ge=15, le=90)
    detail_limit: int = Field(default=18, ge=1, le=30)
    recommend_pages: int = Field(default=2, ge=1, le=3)
    recommend_every_minutes: int = Field(default=120, ge=60)
    max_online_tasks: int = Field(default=24, ge=1, le=40)
    max_controlled_requests: int = Field(default=360, ge=30, le=1000)
    stale_task_minutes: int = Field(default=18, ge=5, le=60)
    jev_batch_limit: int = Field(default=36, ge=1, le=100)
    jev_concurrency: int = Field(default=2, ge=1, le=5)
    jev_max_attempts: int = Field(default=2, ge=1, le=3)
    jev_proxy_url: str = "http://127.0.0.1:7897"
    collect_details: bool = False

    @model_validator(mode="after")
    def bounded_end(self):
        if self.ends_at.tzinfo is None:
            raise ValueError("维护结束时间必须包含时区")
        return self


def utc_now():
    return datetime.now(timezone.utc)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def save_report(plan, output, state, report):
    state["updated_at_utc"] = utc_now().isoformat()
    report["status"] = state["status"]
    write_json(output / "state.json", state)
    write_json(output / "latest.json", report)
    with (output / "cycles.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False) + "\n")
    lines = [f"# {plan.id}", "", f"状态：{state['status']}；检查：{report['observed_at_utc']}；结束：{plan.ends_at.isoformat()}。", "",
             f"主访问：{(report.get('main_access') or {}).get('mode', 'unavailable')}；当前动作：{report['action']}。", "",
             "| 批次 | 列表岗位 | 核验正文 | 状态 |", "|---|---:|---:|---|"]
    for cohort in report["data"]["night_cohorts"]:
        lines.append(f"| {cohort['round_id']} | {cohort['jobs']} | {cohort['verified_bodies']} | {cohort['status']} |")
    if state.get("halted_reason"):
        lines.extend(["", "暂停原因：" + state["halted_reason"]])
    atomic_write(output / "report.md", "\n".join(lines) + "\n")
    return report


def summarize(database, rounds):
    with JobStore(database, read_only=True) as store:
        counts = {"jobs": store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "verified_bodies": store.conn.execute("SELECT count(*) FROM jobs WHERE json_extract(raw_json,'$.detail_access_verified')=1").fetchone()[0]}
        counts["night_cohorts"] = []
        for ident in rounds:
            info = store.conn.execute("SELECT status FROM collection_rounds WHERE round_id=?", (ident,)).fetchone()
            if info:
                rows = store.conn.execute("SELECT row_json FROM collection_observations WHERE round_id=?", (ident,)).fetchall()
                bodies = sum(json.loads(r[0]).get("job_description", "").strip() != "" and
                    json.loads(json.loads(r[0])["raw_json"]).get("detail_access_verified") is True for r in rows)
                counts["night_cohorts"].append({"round_id": ident, "status": info[0], "jobs": len(rows), "verified_bodies": bodies})
        return counts


def tag_verified(plan, database, rounds, output):
    """A bounded decision batch; it never starts or resumes a platform request."""
    from .decisions import ask_decisions, job_questions
    env_path = Path(plan.config).resolve().parent / ".env"
    if not env_path.exists() or not rounds:
        return {"attempted": 0}
    key = next((line.split("=", 1)[1].strip() for line in env_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("BOSS_JEV_API_KEY=")), "")
    if not key:
        return {"attempted": 0, "reason": "Jev key not configured"}
    path = output / "decisions.jsonl"
    recorded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    questions = job_questions()
    question_hash = hashlib.sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    known = {(r["id"], r["body_sha256"], r["question_hash"]) for r in recorded if "error" not in r}
    attempts = {}
    for r in recorded:
        ident = (r["id"], r["body_sha256"], r["question_hash"])
        attempts[ident] = attempts.get(ident, 0) + 1
    with JobStore(database, read_only=True) as store:
        rows = [dict(r) for r in store.conn.execute("SELECT * FROM jobs WHERE json_extract(raw_json,'$.detail_access_verified')=1 AND "
            "canonical_id IN (SELECT canonical_id FROM collection_observations WHERE round_id IN (" + ",".join("?" for _ in rounds) + "))", rounds)]
    pending = []
    for row in rows:
        ident = (row["canonical_id"], hashlib.sha256(row["job_description"].encode()).hexdigest(), question_hash)
        if ident not in known and attempts.get(ident, 0) < plan.jev_max_attempts:
            pending.append(row)
    pending = pending[:plan.jev_batch_limit]
    def call(row):
        state = {k: row[k] for k in ("title", "company_name", "city", "education", "experience", "job_description")}
        result = {"id": row["canonical_id"], "body_sha256": hashlib.sha256(row["job_description"].encode()).hexdigest(),
                  "question_hash": question_hash, "observed_at_utc": utc_now().isoformat()}
        result["attempt"] = attempts.get((result["id"], result["body_sha256"], question_hash), 0) + 1
        try:
            answers, meta = ask_decisions("https://api.futureppo.top/v1", "jev-latest", key, state, questions,
                                         timeout=30, proxy_url=plan.jev_proxy_url)
            result.update(answers=answers, **meta)
        except Exception as exc:
            result["error"] = str(exc).replace(key, "[redacted]")
        return result
    results = []
    with ThreadPoolExecutor(max_workers=plan.jev_concurrency) as pool, path.open("a", encoding="utf-8") as stream:
        for future in as_completed([pool.submit(call, row) for row in pending]):
            result = future.result()
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            results.append(result)
    write_json(output / "questions.json", questions)
    return {"attempted": len(results), "succeeded": sum("error" not in r for r in results),
            "failed": sum("error" in r for r in results)}


def cycle(plan_file: Path, *, inspect_only=False, review_task="", review_reason=""):
    plan_file = plan_file.resolve()
    plan = MaintenancePlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    settings = SettingsStore(plan.config)
    cfg, revision = settings.read()
    database = resolve_database(cfg, settings.path)
    output = Path(plan.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "state.json"
    with file_mutex(output / "cycle.lock"):
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("plan_id") != plan.id:
                raise ValueError("维护状态属于另一计划")
        else:
            state = {"plan_id": plan.id, "started_at_utc": utc_now().isoformat(), "tasks": [], "rounds": [],
                     "keyword_group": -1, "baseline_config": public_config(cfg), "status": "ready"}
        client = ServiceClient(plan.service_url)
        try:
            live = client.request("/api/state")
            listed = client.request("/api/tasks")
        except (ValueError, OSError) as exc:
            report = {"observed_at_utc": utc_now().isoformat(), "ends_at": plan.ends_at.isoformat(),
                      "main_access": None, "active_tasks": None, "owned_tasks": state["tasks"],
                      "data": summarize(database, state["rounds"]), "action": "service_unavailable",
                      "error": redact(str(exc), cfg), "task_stop_confirmed": False}
            if inspect_only:
                return report
            if state["status"] not in ("halted", "finished"):
                state.update(status="halted", halted_reason="本地服务无法读取；任务现场与停止状态需要复核，不自动重启或恢复")
            return save_report(plan, output, state, report)
        active = [t for t in listed["items"] if t["status"] in ACTIVE]
        tasks = {t["id"]: t for t in listed["items"]}
        for entry in state["tasks"]:
            task = tasks.get(entry["id"]) or client.request("/api/tasks/" + entry["id"])
            entry.update(status=task["status"], progress=task["progress"], result=task["result"], error=task["error"],
                         finished_at=task["finished_at"])
        report = {"observed_at_utc": utc_now().isoformat(), "ends_at": plan.ends_at.isoformat(),
                  "main_access": live["safety"], "refresh_access": live.get("refresh_safety"),
                  "active_tasks": [{"id": t["id"], "kind": t["kind"], "status": t["status"]} for t in active],
                  "owned_tasks": state["tasks"], "data": summarize(database, state["rounds"]), "action": "inspect"}
        with JobStore(database, read_only=True) as store:
            if "request_baseline_id" not in state:
                started_local = datetime.fromisoformat(state["started_at_utc"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                state["request_baseline_id"] = store.conn.execute(
                    "SELECT coalesce(max(id),0) FROM request_log WHERE ts < ?", (started_local,)).fetchone()[0]
            requests = store.conn.execute("SELECT count(*) FROM request_log WHERE id>?", (state["request_baseline_id"],)).fetchone()[0]
        report["controlled_requests_since_start"] = requests
        if "baseline_data" not in state:
            first_log = output / "cycles.jsonl"
            first = json.loads(first_log.read_text(encoding="utf-8").splitlines()[0]) if first_log.exists() else report
            state["baseline_data"] = {k: first["data"][k] for k in ("jobs", "verified_bodies")}
        if inspect_only:
            return report
        owned = {t["id"] for t in state["tasks"]}
        def halt(reason):
            state.update(status="halted", halted_reason=reason)
            report["action"] = "halted"
            for task in active:
                if task["id"] in owned:
                    client.request(f"/api/tasks/{task['id']}/stop", {})
        def save():
            return save_report(plan, output, state, report)
        if review_task:
            if not review_reason.strip() or live["safety"]["mode"] != "ready" or not cfg.network_enabled or active:
                raise ValueError("软件故障复核须说明依据、访问ready且没有活动任务；平台暂停不能由维护复核解除")
            failed = next((t for t in state["tasks"] if t["id"] == review_task and t["status"] in ("failed", "interrupted")), None)
            if not failed:
                raise ValueError("复核目标不是本计划失败任务")
            cursor = 0
            while True:
                page = client.request(f"/api/tasks/{review_task}/events?after={cursor}&limit=200")
                if any(e["type"] in ("risk", "verification") for e in page["items"]):
                    raise ValueError("该任务有平台验证或访问限制证据，不能按软件故障放行")
                if not page["items"]:
                    break
                cursor = page["next_cursor"]
            state.setdefault("reviewed_software_failures", {})[review_task] = {"reason": review_reason, "reviewed_at_utc": utc_now().isoformat()}
            state.update(status="ready", halted_reason="")
            report["action"] = "software_failure_reviewed_no_task_started"
            return save()
        if utc_now() >= plan.ends_at:
            for task in active:
                if task["id"] in owned:
                    client.request(f"/api/tasks/{task['id']}/stop", {})
            state["status"] = "finished"
            report["action"] = "finished_at_deadline"
            if state.get("applied_revision") == revision:
                restored, _ = settings.save(state["baseline_config"], revision)
                report["baseline_config_restored"] = True
            else:
                report["baseline_config_restored"] = False
            return save()
        if state["status"] in ("halted", "finished"):
            report["action"] = "no_platform_work"
            return save()
        if live["safety"]["mode"] != "ready" or not cfg.network_enabled:
            halt("平台访问已暂停或关闭；需要人工复检后才能恢复")
            return save()
        if any(t["status"] == "waiting_verification" for t in active):
            halt("检测到平台登录/验证码/访问验证；夜间任务停止并保留持久暂停")
            return save()
        failures = [t for t in state["tasks"] if t["status"] in ("failed", "interrupted")
                    and t["id"] not in state.get("reviewed_software_failures", {})]
        if failures:
            halt("夜间任务失败，保留成功结果并等待故障复核：" + redact(failures[-1]["error"], cfg))
            return save()
        if requests >= plan.max_controlled_requests:
            for task in active:
                if task["id"] in owned:
                    client.request(f"/api/tasks/{task['id']}/stop", {})
            state["status"] = "finished"
            report["action"] = "local_request_budget_finished"
            return save()
        if active:
            for task in active:
                if task["id"] in owned and task.get("started_at"):
                    age = (utc_now() - datetime.fromisoformat(task["started_at"])).total_seconds() / 60
                    if age > plan.stale_task_minutes:
                        halt("本批超过有界运行窗口，停止资源并等待复核")
                        return save()
            report["action"] = "monitor_active_task"
            return save()
        if len(state["tasks"]) >= plan.max_online_tasks:
            state["status"] = "finished"
            report["action"] = "local_work_budget_finished"
            return save()
        # Configuration changes are revision-checked. A paused platform is never released here.
        payload = cfg.model_dump(mode="json")
        payload.update(list_source="native", pages_per_query=1, pages_per_keyword={}, collect_details=False,
                       request_delay_seconds=[8, 12], detail_delay_seconds=[8, 12], detail_batch=4,
                       detail_cooldown_seconds=[35, 50], burst_threshold=20, burst_penalty_seconds=[50, 80])
        successful_details = [t for t in state["tasks"] if t["kind"] == "details" and t["status"] == "succeeded"]
        detail_limit = min(24, plan.detail_limit + 3 * (len(successful_details) // 3))
        previous = state["tasks"][-1] if state["tasks"] else None
        kind = "collect"
        options = {"limit": plan.collect_limit}
        if previous and previous["kind"] in ("collect", "recommend"):
            ident = previous["round_id"]
            from .service_jobs import selected_rows
            with JobStore(database, read_only=True) as store:
                ctx = SimpleNamespace(task={"options": {"round_id": ident, "limit": detail_limit}}, initial=cfg)
                pending = selected_rows(ctx, store, "details", cfg)
            if pending:
                kind, options = "details", {"limit": detail_limit, "round_id": ident}
        if kind == "collect":
            last_recommend = state.get("last_recommend_at")
            due = not last_recommend or (utc_now() - datetime.fromisoformat(last_recommend)).total_seconds() >= plan.recommend_every_minutes * 60
            if due:
                ident = f"{plan.id}-rec-{len(state['tasks']):02d}"
                kind, options = "recommend", {"limit": plan.recommend_pages * 15, "round_id": ident, "recommend_pages": plan.recommend_pages}
                state["last_recommend_at"] = utc_now().isoformat()
            else:
                ident = state.get("keyword_round")
                info = next((r for r in report["data"]["night_cohorts"] if r["round_id"] == ident), None)
                if not ident or info and info["status"] == "complete":
                    state["keyword_group"] = (state["keyword_group"] + 1) % len(plan.keyword_groups)
                    ident = f"{plan.id}-kw-{len(state['tasks']):02d}"
                    state["keyword_round"] = ident
                payload["keywords"] = plan.keyword_groups[state["keyword_group"]]
                options["round_id"] = ident
        actual, applied = settings.save(payload, revision)
        state["applied_revision"] = applied
        created = client.request("/api/tasks", {"kind": kind, "options": options})
        state["tasks"].append({"id": created["id"], "kind": kind, "round_id": options["round_id"],
                               "status": created["status"], "progress": created["progress"]})
        if options["round_id"] not in state["rounds"]:
            state["rounds"].append(options["round_id"])
        state["status"] = "running"
        report["action"] = "started_" + kind
        report["new_task"] = {"id": created["id"], "kind": kind, "options": options}
        report["parameter_decision"] = {"spacing": [8, 12], "detail_limit": detail_limit,
            "basis": "bounded starting batch; +3 details after three successful detail batches", "keyword_group": state["keyword_group"]}
        # Only completed, already verified data are sent to Jev; writes remain bounded.
        # Persist task ownership before calling an independent model endpoint.
        state["updated_at_utc"] = utc_now().isoformat()
        write_json(state_path, state)
        try:
            report["jev"] = tag_verified(plan, database, state["rounds"], output)
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            report["jev"] = {"error": redact(str(exc), cfg)}
        return save()
