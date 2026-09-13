"""Loopback service console and shared task-control API."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from .analysis.decisions import BUCKETS
from .analysis.market import MarketSnapshot, csv_export
from .candidate import CandidateProfile
from .settings import APPLY_POLICY, RevisionConflict, SettingsStore, resolve_database
from .service_config import public_config, secret_status
from .safety import AccessBlocked, AccessGate
from .tasks import TaskManager
from .task_store import TaskConflict
from .annotations import latest_annotations, annotation_stale
from .storage import JobStore

VERSION = "0.3.0"
WEB = Path(__file__).parent / "web"
MAX_BODY = 1024 * 1024


class LocalNotes:
    """Manual tracking in a sidecar DB; imported job evidence is never overwritten."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    canonical_id TEXT PRIMARY KEY, bookmarked INTEGER,
                    applied INTEGER, note TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_history (
                    id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL,
                    action TEXT NOT NULL, value_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def all(self) -> dict[str, dict]:
        conn = self.connect()
        try:
            return {row["canonical_id"]: {k: (bool(row[k]) if k in ("bookmarked", "applied") else row[k])
                                          for k in ("bookmarked", "applied", "note") if row[k] is not None}
                    for row in conn.execute("SELECT * FROM notes")}
        finally:
            conn.close()

    def update(self, cid: str, action: str, value) -> None:
        if action not in ("bookmark", "applied", "note", "reset"):
            raise ValueError("未知记录动作")
        if action in ("bookmark", "applied") and not isinstance(value, bool):
            raise ValueError("收藏与已投状态必须为布尔值")
        if action == "note" and (not isinstance(value, str) or len(value) > 3000):
            raise ValueError("备注应为不超过 3000 字的文本")
        stamp = datetime.now(timezone.utc).isoformat()
        conn = self.connect()
        try:
            with conn:
                conn.execute("INSERT INTO notes(canonical_id,updated_at) VALUES (?,?) ON CONFLICT(canonical_id) DO NOTHING", (cid, stamp))
                if action == "reset":
                    conn.execute("UPDATE notes SET bookmarked=0,applied=0,note='',updated_at=? WHERE canonical_id=?", (stamp, cid))
                else:
                    column = {"bookmark": "bookmarked", "applied": "applied", "note": "note"}[action]
                    conn.execute(f"UPDATE notes SET {column}=?,updated_at=? WHERE canonical_id=?", (value, stamp, cid))
                conn.execute("INSERT INTO action_history(canonical_id,action,value_json,created_at) VALUES (?,?,?,?)",
                             (cid, action, json.dumps(value, ensure_ascii=False), stamp))
        finally:
            conn.close()


class Workbench:
    def __init__(self, config_path: str | Path):
        self.settings = SettingsStore(config_path)
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        cfg, _ = self.settings.read()
        self.database = resolve_database(cfg, self.settings.path)
        if not self.database.exists():
            with JobStore(self.database):
                pass
        self.snapshot = MarketSnapshot(self.database)
        self.notes = LocalNotes(self.database.parent / "review-state.sqlite3")
        self.fingerprint = self._fingerprint()
        self.tasks = TaskManager(self.settings)
        self.annotations = self._annotations()

    def _annotations(self):
        with JobStore(self.database, read_only=True) as store:
            return latest_annotations(store.conn)

    def _fingerprint(self):
        # WAL writes may not change the main database mtime.
        return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                     for p in (self.database, Path(str(self.database) + "-wal")))

    def current(self) -> MarketSnapshot:
        with self.lock:
            fp = self._fingerprint()
            day = datetime.now(timezone.utc).date()
            if fp != self.fingerprint or self.snapshot.now.date() != day:
                self.snapshot = MarketSnapshot(self.database)
                self.annotations = self._annotations()
                self.fingerprint = fp
            return self.snapshot

    def state(self):
        from .safety import AccessGate
        cfg, revision = self.settings.read()
        safety = AccessGate(self.database).read_state().as_dict()
        if not cfg.network_enabled and safety.get("mode") == "ready":
            safety = {**safety, "mode": "offline", "reason": "在线访问已关闭"}
        return {"config": public_config(cfg), "revision": revision, "csrf_token": self.csrf,
                "safety": safety, "version": VERSION, "secrets": secret_status(cfg), "apply_policy": APPLY_POLICY}

    def annotate(self, items: list[dict]) -> list[dict]:
        saved = self.notes.all()
        cfg, _ = self.settings.read()
        result = []
        for item in items:
            annotation = self.annotations.get(item["id"])
            if annotation:
                annotation = {**annotation, "stale": annotation_stale(self.snapshot.by_id[item["id"]], cfg, annotation)}
            result.append({**item, **saved.get(item["id"], {}), "annotation": annotation})
        return result


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, workbench: Workbench):
        self.workbench = workbench
        super().__init__(("127.0.0.1", port), Handler)

    def server_close(self):
        self.workbench.tasks.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def _allowed_host(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host", "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        # Keep each request bounded; malformed POST bodies cannot leak into a following request.
        self.send_header("Connection", "close")
        self.close_connection = True
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    @staticmethod
    def _filter(query: dict) -> dict:
        fields = {key: query.get(key, [""])[0] for key in ("q", "city", "tier")}
        if len(fields["q"]) > 200 or len(fields["city"]) > 100 or (fields["tier"] and fields["tier"] not in BUCKETS):
            raise ValueError("筛选参数无效")
        return fields

    def do_GET(self):
        if not self._allowed_host():
            self._json(403, {"error": "只接受本机来源"})
            return
        app = self.server.workbench
        try:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            query = parse_qs(parts.query, max_num_fields=20)
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            for asset in WEB.iterdir():
                if asset.is_file() and asset.suffix in (".js", ".css"):
                    static["/" + asset.name] = (asset.name, "text/javascript; charset=utf-8" if asset.suffix == ".js" else "text/css; charset=utf-8")
            if path in static:
                name, ctype = static[path]
                self._send(200, (WEB / name).read_bytes(), ctype)
                return
            if path == "/api/state":
                self._json(200, app.state())
                return
            cfg, _ = app.settings.read()
            if path == "/api/options":
                from .options import option_catalog
                self._json(200, option_catalog(cfg, app.current().rows))
                return
            if path == "/api/tasks":
                self._json(200, app.tasks.list())
                return
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    self._json(200, app.tasks.store.get(parts[2]))
                elif len(parts) == 4 and parts[3] == "events":
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["200"])[0])
                    if after < 0 or not 1 <= limit <= 500:
                        raise ValueError("日志游标或条数无效")
                    self._json(200, app.tasks.store.events(parts[2], after, limit))
                else:
                    self._json(404, {"error": "未找到操作"})
                return
            profile = cfg.candidate or CandidateProfile()
            snap = app.current()
            if path == "/api/overview":
                overview = dict(snap.overview())
                overview["scope"] = "本地岗位池 · 全部已采集记录"
                totals = dict(overview["totals"])
                totals["tagged"] = len({row["canonical_id"] for row in snap.rows if row.get("glm_tags_json") not in (None, "", "{}")}.union(app.annotations))
                overview["totals"] = totals
                self._json(200, overview)
            elif path == "/api/keywords":
                self._json(200, snap.keywords(cfg))
            elif path == "/api/jobs":
                page = int(query.get("page", ["1"])[0])
                size = int(query.get("page_size", ["30"])[0])
                if not (1 <= page <= 100000 and 1 <= size <= 100):
                    raise ValueError("页码或每页条数无效")
                chosen = snap.select(profile, **self._filter(query))
                items = [snap.item(row, decision) for row, decision in chosen[(page - 1) * size:page * size]]
                self._json(200, {"total": len(chosen), "page": page, "page_size": size, "items": app.annotate(items)})
            elif path.startswith("/api/jobs/"):
                cid = path[len("/api/jobs/"):]
                if cid not in snap.by_id:
                    self._json(404, {"error": "未找到岗位"})
                    return
                from .analysis.decisions import evaluate
                row = snap.by_id[cid]
                self._json(200, app.annotate([snap.item(row, evaluate(row, profile, snap.now), details=True)])[0])
            elif path == "/api/export/jobs.csv":
                items = [snap.item(row, decision) for row, decision in snap.select(profile, **self._filter(query))]
                self._send(200, csv_export(items), "text/csv; charset=utf-8", {"Content-Disposition": 'attachment; filename="job-review-queue.csv"'})
            else:
                self._json(404, {"error": "未找到页面"})
        except KeyError:
            self._json(404, {"error": "未找到任务"})
        except (ValueError, FileNotFoundError) as exc:
            self._json(400, {"error": str(exc)[:200]})
        except Exception:
            self._json(500, {"error": "无法读取本地数据，请在终端检查配置或数据库"})

    def do_POST(self):
        port = self.server.server_port
        origin = self.headers.get("Origin")
        allowed_origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        if (not self._allowed_host() or (origin is not None and origin not in allowed_origins)
                or self.headers.get("Sec-Fetch-Site") == "cross-site"
                or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.workbench.csrf)):
            self._json(403, {"error": "请求来源或本机校验令牌无效，请重新载入页面"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "仅接受 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                self._json(413, {"error": "请求体为空或过大"})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须为对象")
            path = unquote(urlsplit(self.path).path)
            app = self.server.workbench
            if path == "/api/config":
                app.settings.save(payload.get("config"), payload.get("revision", ""), payload.get("clear_secrets"))
                self._json(200, app.state())
            elif path == "/api/tasks":
                self._json(202, app.tasks.start(payload.get("kind"), payload.get("options")))
            elif path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    self._json(404, {"error": "未找到操作"})
                elif parts[3] == "stop":
                    self._json(200, app.tasks.stop(parts[2]))
                elif parts[3] == "resume":
                    self._json(200, app.tasks.resume(parts[2], payload.get("confirmed") is True))
                elif parts[3] == "verification":
                    self._json(200, app.tasks.show_verification(parts[2]))
                else:
                    self._json(404, {"error": "未找到操作"})
            elif path == "/api/access":
                if payload.get("action") == "pause":
                    reason = payload.get("reason", "手动暂停")
                    if not isinstance(reason, str) or len(reason) > 500:
                        raise ValueError("暂停原因最多 500 字")
                    AccessGate(app.database).pause(reason)
                    for task in app.tasks.store.active():
                        if task["kind"] != "tag" and not task["options"]["dry_run"]:
                            app.tasks.stop(task["id"])
                elif payload.get("action") == "release":
                    if payload.get("confirmed") is not True:
                        raise ValueError("请先确认平台状态已恢复")
                    confirmation = payload.get("confirmation", "用户通过管理页确认平台状态已恢复")
                    if not isinstance(confirmation, str) or not confirmation.strip() or len(confirmation) > 500:
                        raise ValueError("恢复依据需为 1–500 字")
                    AccessGate(app.database).release(confirmation)
                else:
                    raise ValueError("访问状态操作无效")
                self._json(200, app.state())
            elif path.startswith("/api/jobs/") and path.endswith("/action"):
                cid = path[len("/api/jobs/"):-len("/action")]
                if cid not in app.current().by_id:
                    self._json(404, {"error": "未找到岗位，未写入记录"})
                    return
                app.notes.update(cid, payload.get("action"), payload.get("value"))
                self._json(200, {"ok": True, "local_only": True})
            else:
                self._json(404, {"error": "未找到操作"})
        except (RevisionConflict, TaskConflict, AccessBlocked) as exc:
            self._json(409, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "未找到任务"})
        except ValidationError as exc:
            # Do not echo whole config values in validation errors.
            issues = [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors(include_input=False, include_url=False)]
            self._json(422, {"error": "；".join(issues)[:700]})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)[:200]})
        except OSError:
            self._json(409, {"error": "本地文件正被使用或无法保存，请检查文件权限后重试"})
        except sqlite3.OperationalError:
            self._json(503, {"error": "本地记录数据库繁忙或不可写，请稍后再试"})
        except Exception:
            self._json(500, {"error": "操作失败，请查看服务日志"})


def serve(config: str, port: int = 8765):
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535")
    app = Workbench(config)
    server = WorkbenchServer(port, app)
    print(f"JobMarketRadar 数据服务：http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
