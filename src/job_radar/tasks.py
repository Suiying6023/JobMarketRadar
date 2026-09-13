"""One task API for the web console, CLI and Python callers."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .local_files import file_mutex
from .processes import ProcessTree
from .safety import AccessGate
from .service_config import redact
from .settings import SettingsStore, resolve_database
from .task_store import ACTIVE, KINDS, TERMINAL, TaskConflict, TaskOptions, TaskStore, now


class TaskManager:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        cfg, _ = settings.read()
        self.database = resolve_database(cfg, settings.path)
        self.store = TaskStore(self.database.parent / "service-state.sqlite3", cfg.service.event_retention)
        self._guard = file_mutex(self.store.path.with_suffix(".manager.lock"))
        try:
            self._guard.__enter__()
        except (OSError, BlockingIOError) as exc:
            raise TaskConflict("已有服务在管理此数据池，请使用其 API 或停止后再启动") from exc
        self._lock = threading.RLock()
        self._workers: dict[str, tuple[subprocess.Popen, ProcessTree]] = {}
        self._readers: dict[str, threading.Thread] = {}
        self._closed = False
        try:
            self.store.recover()
        except Exception:
            self._guard.__exit__(None, None, None)
            raise

    def start(self, kind: str, options: dict | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError("任务类型无效")
        opts = TaskOptions.model_validate(options or {})
        cfg, revision = self.settings.read()
        self.store.retention = cfg.service.event_retention
        if kind == "collect" and any(not city.boss_code for city in cfg.cities):
            raise ValueError("采集城市缺少平台城市码，请在配置中补充")
        if not opts.dry_run:
            if kind == "tag":
                if not cfg.llm.enabled or not cfg.llm.model.strip():
                    raise TaskConflict("请先配置并启用 LLM 端点与模型")
            else:
                AccessGate(self.database).require_online(cfg.network_enabled)
        with self._lock:
            if self._closed:
                raise TaskConflict("服务正在停止")
            resource = ("preview" if opts.dry_run else "llm" if kind == "tag" else "browser")
            task = self.store.create(kind, opts, revision, resource)
            ident = task["id"]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONUTF8"] = "1"
            command = [sys.executable, "-X", "utf8", "-m", "job_radar.worker", "--config", str(self.settings.path),
                       "--store", str(self.store.path), "--task", ident, "--parent", str(os.getpid())]
            proc = None
            tree = None
            try:
                tree = ProcessTree.spawn(command, cwd=self.settings.path.parent, env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                         errors="replace")
                proc = tree.process
                self._workers[ident] = (proc, tree)
                self.store.update(ident, status="running", started_at=now())
                self.store.event(ident, "state", f"开始{KINDS[kind]}")
                reader = threading.Thread(target=self._watch, args=(ident, proc, tree), daemon=True)
                self._readers[ident] = reader
                reader.start()
            except Exception as exc:
                if tree is not None:
                    tree.close()
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc is not None and proc.stdout:
                    proc.stdout.close()
                self._workers.pop(ident, None)
                self._readers.pop(ident, None)
                self.store.finish(ident, "failed", error=redact(f"启动失败：{type(exc).__name__}", cfg))
                raise
        return self.store.get(ident)

    def _watch(self, ident, proc, tree):
        try:
            if proc.stdout:
                for line in proc.stdout:
                    try:
                        cfg, _ = self.settings.read()
                        message = redact(line.strip(), cfg)
                        if message:
                            self.store.event(ident, "log", message)
                    except (OSError, ValueError):
                        self.store.event(ident, "log", "工作进程输出无法读取配置，详细内容已省略", level="warning")
            code = proc.wait()
            current = self.store.get(ident)
            if current["status"] not in TERMINAL:
                status = "cancelled" if self.store.commands(ident).get("stop_requested") else "failed"
                self.store.finish(ident, status, error="" if status == "cancelled" else f"工作进程退出（{code}）")
        finally:
            if proc.stdout:
                proc.stdout.close()
            tree.close()
            with self._lock:
                self._workers.pop(ident, None)
                self._readers.pop(ident, None)

    def stop(self, ident: str) -> dict:
        task = self.store.get(ident)
        if task["status"] in TERMINAL:
            return task
        self.store.command(ident, "stop")
        self.store.update(ident, status="stopping")
        self.store.event(ident, "state", "正在停止并关闭任务资源")
        threading.Thread(target=self._stop_deadline, args=(ident,), daemon=True).start()
        return self.store.get(ident)

    def _stop_deadline(self, ident):
        try:
            cfg, _ = self.settings.read()
            timeout = cfg.service.stop_timeout_seconds
        except (ValueError, OSError):
            timeout = 10
        with self._lock:
            worker = self._workers.get(ident)
        if worker:
            proc, tree = worker
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                tree.kill()

    def resume(self, ident: str, confirmed: bool = False) -> dict:
        task = self.store.get(ident)
        if task["status"] != "waiting_verification":
            raise TaskConflict("任务当前没有待处理的验证")
        if confirmed is not True:
            raise ValueError("请确认已在任务浏览器中完成验证")
        cfg, _ = self.settings.read()
        if not cfg.network_enabled:
            raise TaskConflict("请先在配置中启用在线访问")
        gate = AccessGate(self.database)
        state = gate.read_state()
        if not state.ready:
            gate.release("用户确认完成验证，按实际业务响应复检", expected_source=f"managed-task:{ident}")
        self.store.event(ident, "access", "用户确认继续", data={"previous_reason": state.reason, "previous_source": state.source})
        self.store.command(ident, "resume")
        self.store.event(ident, "state", "已收到继续指令，将复检当前请求")
        return self.store.get(ident)

    def show_verification(self, ident: str) -> dict:
        task = self.store.get(ident)
        if task["status"] != "waiting_verification":
            raise TaskConflict("任务当前没有待处理的验证")
        self.store.command(ident, "show")
        return task

    def list(self) -> dict:
        items = self.store.list()
        return {"items": items, "active_count": len(self.store.active())}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers.items())
            readers = list(self._readers.values())
        for ident, _ in workers:
            self.store.command(ident, "stop")
        deadline = time.monotonic() + 10
        for ident, (proc, tree) in workers:
            try:
                proc.wait(timeout=max(0.05, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                tree.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            if self.store.get(ident)["status"] not in TERMINAL:
                self.store.finish(ident, "cancelled", error="服务关闭")
            tree.close()
        for reader in readers:
            reader.join(timeout=3)
        self._guard.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
