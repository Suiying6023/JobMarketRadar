"""Durable task state and cursor-based events shared by workers and clients."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


KINDS = {"collect": "采集岗位", "recommend": "推荐岗位", "details": "补全详情", "refresh": "刷新状态", "tag": "LLM 打标"}
ACTIVE = ("queued", "running", "waiting_verification", "stopping")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class TaskConflict(ValueError):
    pass


class TaskOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=100, ge=1, le=100000)
    dry_run: bool = False
    round_id: str = Field(default="", pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    canonical_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=1000)
    recommend_pages: int = Field(default=2, ge=1, le=6)
    expectation_label: str = Field(default="", max_length=100)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, path: Path | str, retention: int = 20000):
        self.path = Path(path).resolve()
        self.retention = retention
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    resource TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, config_revision TEXT NOT NULL, applied_revision TEXT NOT NULL,
                    options TEXT NOT NULL, progress TEXT NOT NULL, result TEXT NOT NULL,
                    verification TEXT, error TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    resume_requested INTEGER NOT NULL DEFAULT 0,
                    show_requested INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_resource ON tasks(resource)
                    WHERE status IN ('queued','running','waiting_verification','stopping');
                CREATE TABLE IF NOT EXISTS task_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, type TEXT NOT NULL, level TEXT NOT NULL,
                    message TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON task_events(task_id,seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row) -> dict:
        value = dict(row)
        for key in ("options", "progress", "result", "verification"):
            value[key] = json.loads(value[key]) if value[key] else None
        for key in ("resource", "stop_requested", "resume_requested", "show_requested"):
            value.pop(key, None)
        return value

    def create(self, kind: str, options: TaskOptions, revision: str, resource: str) -> dict:
        ident = uuid.uuid4().hex
        try:
            with self.connect() as db:
                db.execute("INSERT INTO tasks(id,kind,status,resource,created_at,config_revision,applied_revision,options,progress,result) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (ident, kind, "queued", resource, now(), revision, revision,
                            options.model_dump_json(), '{"processed":0,"total":0,"failed":0}', '{}'))
        except sqlite3.IntegrityError as exc:
            raise TaskConflict("同一数据池的该类任务正在运行，请等待完成或停止当前任务") from exc
        self.event(ident, "state", "任务已创建")
        return self.get(ident)

    def get(self, ident: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return self.decode(row)

    def list(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY CASE WHEN status IN ('queued','running','waiting_verification','stopping') THEN 0 ELSE 1 END,created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.decode(r) for r in rows]

    def active(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks WHERE status IN ('queued','running','waiting_verification','stopping') ORDER BY created_at").fetchall()
        return [self.decode(row) for row in rows]

    def update(self, ident: str, **fields) -> dict:
        allowed = {"status", "started_at", "finished_at", "applied_revision", "progress", "result", "verification", "error"}
        if not fields or fields.keys() - allowed:
            raise ValueError("任务字段无效")
        if "status" in fields and fields["status"] not in ACTIVE + TERMINAL:
            raise ValueError("任务状态无效")
        values = {k: json.dumps(v, ensure_ascii=False) if k in ("progress", "result", "verification") and v is not None else v
                  for k, v in fields.items()}
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {','.join(k+'=?' for k in values)} WHERE id=? AND status NOT IN ('succeeded','failed','cancelled','interrupted')",
                       (*values.values(), ident))
        return self.get(ident)

    def finish(self, ident: str, status: str, *, error: str = "", result: dict | None = None):
        """把「状态变终态」和「终态事件」写进**同一个事务**。

        这两件事以前是两次独立提交，中间存在一个窗口：任务已经能被读成 succeeded，
        但「任务完成」这条事件还没落库。轮询方（网页、外部脚本、测试）的典型写法是
        「等到终态 → 读事件」，正好卡在窗口里就会看到事件流在终态前一条断掉；
        按游标分页的调用方还会在稍后多读出一条，看起来像分页错乱。
        全量测试并发跑的时候这个窗口被放大，能被稳定命中。
        """
        if status not in TERMINAL:
            raise ValueError("终态无效")
        label = {"succeeded": "任务完成", "failed": "任务失败",
                 "cancelled": "任务已停止", "interrupted": "任务已中断"}[status]
        level = "error" if status == "failed" else "info"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tasks SET status=?,finished_at=?,error=?,result=?,verification=NULL "
                "WHERE id=? AND status NOT IN ('succeeded','failed','cancelled','interrupted')",
                (status, now(), error, json.dumps(result or {}, ensure_ascii=False), ident),
            )
            db.execute(
                "INSERT INTO task_events(task_id,created_at,type,level,message,data) VALUES (?,?,?,?,?,?)",
                (ident, now(), "state", level, label[:4000], "{}"),
            )
        return self.get(ident)

    def command(self, ident: str, command: str):
        column = {"stop": "stop_requested", "resume": "resume_requested", "show": "show_requested"}.get(command)
        if column is None:
            raise ValueError("任务命令无效")
        task = self.get(ident)
        if task["status"] in TERMINAL:
            return
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {column}=1 WHERE id=?", (ident,))

    def commands(self, ident: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT stop_requested,resume_requested,show_requested FROM tasks WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else {"stop_requested": 1}

    def consume(self, ident: str, command: str) -> bool:
        column = {"resume": "resume_requested", "show": "show_requested"}[command]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(f"SELECT {column} FROM tasks WHERE id=?", (ident,)).fetchone()[0]
            db.execute(f"UPDATE tasks SET {column}=0 WHERE id=?", (ident,))
        return bool(found)

    def event(self, ident: str, kind: str, message: str, *, level: str = "info", data: dict | None = None):
        with self.connect() as db:
            cursor = db.execute("INSERT INTO task_events(task_id,created_at,type,level,message,data) VALUES (?,?,?,?,?,?)",
                                (ident, now(), kind, level, message[:4000], json.dumps(data or {}, ensure_ascii=False)))
            seq = cursor.lastrowid or 0
            if seq % 100 == 0:
                db.execute("DELETE FROM task_events WHERE seq < ?", (max(0, seq-self.retention),))

    def events(self, ident: str, after: int = 0, limit: int = 200) -> dict:
        self.get(ident)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM task_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (ident, after, limit)).fetchall()
        items = [{**dict(row), "data": json.loads(row["data"])} for row in rows]
        return {"items": items, "next_cursor": items[-1]["seq"] if items else after}

    def recover(self):
        for task in self.active():
            self.finish(task["id"], "interrupted", error="服务重启，任务可重新发起")
