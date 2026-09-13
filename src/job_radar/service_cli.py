"""Structured Agent entry points; lifecycle and work use the same service core."""
from __future__ import annotations

import json
import time
import sys
from pathlib import Path

import typer

from .client import ServiceClient
from .settings import SettingsStore
from .task_store import TERMINAL
from .tasks import TaskManager

tasks = typer.Typer(help="任务启停、状态和日志（JSON）")
settings = typer.Typer(help="服务配置（JSON）")


def output(value):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False), flush=True)


def request(url, path, payload=None):
    try:
        result = ServiceClient(url).request(path, payload)
    except ValueError as exc:
        output({"error": str(exc)})
        raise typer.Exit(1)
    output(result)


@tasks.command("start")
def start(kind: str, limit: int = 100, dry_run: bool = False, url: str = "http://127.0.0.1:8765"):
    request(url, "/api/tasks", {"kind": kind, "options": {"limit": limit, "dry_run": dry_run}})


@tasks.command("list")
def list_tasks(url: str = "http://127.0.0.1:8765"):
    request(url, "/api/tasks")


@tasks.command("show")
def show(ident: str, url: str = "http://127.0.0.1:8765"):
    request(url, "/api/tasks/" + ident)


@tasks.command("stop")
def stop(ident: str, url: str = "http://127.0.0.1:8765"):
    request(url, f"/api/tasks/{ident}/stop", {})


@tasks.command("resume")
def resume(ident: str, confirmed: bool = False, url: str = "http://127.0.0.1:8765"):
    request(url, f"/api/tasks/{ident}/resume", {"confirmed": confirmed})


@tasks.command("verify-window")
def verify_window(ident: str, url: str = "http://127.0.0.1:8765"):
    request(url, f"/api/tasks/{ident}/verification", {})


@tasks.command("events")
def events(ident: str, after: int = 0, url: str = "http://127.0.0.1:8765"):
    request(url, f"/api/tasks/{ident}/events?after={after}")


@settings.command("get")
def config_get(url: str = "http://127.0.0.1:8765"):
    request(url, "/api/state")


@settings.command("set")
def config_set(file: Path, url: str = "http://127.0.0.1:8765"):
    """文件内容为 {config,revision,clear_secrets?}，revision 来自 config get。"""
    try:
        payload = json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        output({"error": f"配置输入文件无效：{type(exc).__name__}"})
        raise typer.Exit(1)
    request(url, "/api/config", payload)


def run_task(kind: str, config: str = "config.local.yaml", limit: int = 100, dry_run: bool = False):
    """直接运行一个任务，结束时输出 JSON；与网页使用同一任务核心。"""
    task = None
    try:
        with TaskManager(SettingsStore(config)) as manager:
            task = manager.start(kind, {"limit": limit, "dry_run": dry_run})
            try:
                while manager.store.get(task["id"])["status"] not in TERMINAL:
                    if manager.store.get(task["id"])["status"] == "waiting_verification":
                        manager.store.event(task["id"], "verification", "独立模式结束等待；交互验证请使用 serve 与 task 命令")
                        manager.stop(task["id"])
                        break
                    time.sleep(0.2)
            except KeyboardInterrupt:
                manager.stop(task["id"])
            finally:
                manager.close()
            task = manager.store.get(task["id"])
            output(task)
    except Exception as exc:
        output({"error": f"{type(exc).__name__}: {exc}"})
        raise typer.Exit(1)
    if task and task["status"] != "succeeded":
        raise typer.Exit(2)
