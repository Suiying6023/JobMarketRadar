"""Versioned model annotations and status observations alongside the job store."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS job_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_id TEXT NOT NULL,
    input_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL, provider TEXT NOT NULL,
    model TEXT NOT NULL, config_revision TEXT NOT NULL, task_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, usage_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(canonical_id,input_hash,prompt_hash,provider,model));
CREATE INDEX IF NOT EXISTS annotation_job ON job_annotations(canonical_id,id);
CREATE TABLE IF NOT EXISTS annotation_prompts (
    prompt_hash TEXT PRIMARY KEY, prompt_text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_status_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_id TEXT NOT NULL,
    task_id TEXT NOT NULL, checked_at TEXT NOT NULL, outcome TEXT NOT NULL,
    evidence_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS checks_job ON job_status_checks(canonical_id,id);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    with db:
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(job_annotations)")}
        if "input_json" not in columns:
            db.execute("ALTER TABLE job_annotations ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}'")


def hash_json(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def annotation_key(row: dict, cfg) -> tuple[str, str, str, str]:
    source = {"input": annotation_input(row, cfg), "source_text": row.get("job_description") or ""}
    return (hash_json(source), hash_json(cfg.llm.system_prompt), cfg.llm.base_url, cfg.llm.model)


def annotation_input(row: dict, cfg) -> dict:
    return {"title": row.get("title", ""), "company": row.get("company_name", ""),
            "city": row.get("city", ""), "education": row.get("education", ""), "experience": row.get("experience", ""),
            "job_description": (row.get("job_description") or "")[:cfg.llm.max_input_chars],
            "input_truncated": len(row.get("job_description") or "") > cfg.llm.max_input_chars,
            "target_roles": cfg.candidate.roles if cfg.candidate else [],
            "skill_keywords": cfg.candidate.skills if cfg.candidate else []}


def has_annotation(db, row, cfg):
    return db.execute("SELECT 1 FROM job_annotations WHERE canonical_id=? AND input_hash=? AND prompt_hash=? AND provider=? AND model=?",
                      (row["canonical_id"], *annotation_key(row, cfg))).fetchone() is not None


def save_annotation(db, row, cfg, revision, task_id, payload, usage):
    with db:
        stamp = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT OR IGNORE INTO annotation_prompts(prompt_hash,prompt_text,created_at) VALUES (?,?,?)",
                   (hash_json(cfg.llm.system_prompt), cfg.llm.system_prompt, stamp))
        db.execute("INSERT INTO job_annotations(canonical_id,input_hash,prompt_hash,provider,model,config_revision,task_id,payload_json,usage_json,created_at,input_json) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_id,input_hash,prompt_hash,provider,model) DO NOTHING",
                   (row["canonical_id"], *annotation_key(row, cfg), revision, task_id,
                    json.dumps(payload, ensure_ascii=False), json.dumps(usage or {}, ensure_ascii=False), stamp,
                    json.dumps(annotation_input(row, cfg), ensure_ascii=False)))


def latest_annotations(db) -> dict:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_annotations'").fetchone():
        return {}
    rows = db.execute("SELECT * FROM job_annotations WHERE id IN (SELECT MAX(id) FROM job_annotations GROUP BY canonical_id)").fetchall()
    result = {}
    for source in rows:
        row = dict(source)
        result[row["canonical_id"]] = {"payload": json.loads(row["payload_json"]), "model": row["model"],
            "provider": row["provider"], "created_at": row["created_at"], "prompt_hash": row["prompt_hash"], "input_hash": row["input_hash"]}
    return result


def annotation_stale(row, cfg, annotation):
    return annotation_key(row, cfg) != tuple(annotation.get(k) for k in ("input_hash", "prompt_hash", "provider", "model"))


def record_status(db, row, task_id, evidence, invalid: bool | None):
    stamp = datetime.now(timezone.utc).isoformat()
    outcome = "unknown" if invalid is None else "closed" if invalid else "open"
    with db:
        db.execute("INSERT INTO job_status_checks(canonical_id,task_id,checked_at,outcome,evidence_json) VALUES (?,?,?,?,?)",
                   (row["canonical_id"], task_id, stamp, outcome, json.dumps(evidence, ensure_ascii=False)))
        if invalid is not None:
            # A status check never changes the timestamp of the stored JD body.
            db.execute("UPDATE jobs SET job_invalid=?,job_status_desc=?,updated_at=? WHERE canonical_id=?",
                       (int(invalid), evidence.get("job_status") or ("职位已关闭" if invalid else "招聘中"), stamp, row["canonical_id"]))


def last_status_times(db) -> dict:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_status_checks'").fetchone():
        return {}
    return dict(db.execute("SELECT canonical_id,MAX(checked_at) FROM job_status_checks WHERE outcome IN ('open','closed') GROUP BY canonical_id").fetchall())
