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
def start(kind: str, limit: int = 100, dry_run: bool = False, round_id: str = "", options: str = "",
          url: str = "http://127.0.0.1:8765"):
    """启动任务。`options` 是 JSON 对象字符串，用来传任务种类专属选项。

    为什么要有这个口子：`session`（采集会话）需要 phase / pages / detail_limit /
    keywords 这些选项，而通用三项（limit / dry_run / round_id）表达不了。让 Agent
    去拼 HTTP 请求体也行，但那样每次都得重新确认 CSRF 与请求头；这里收一个 JSON
    字符串，仍然只有一个入口：

        job-radar task start session --options '{"phase":"sweep","pages":2}'
    """
    extra: dict = {}
    if options.strip():
        try:
            extra = json.loads(options)
        except ValueError:
            output({"error": "options 需为 JSON 对象字符串，例如 {\"phase\":\"sweep\"}"})
            raise typer.Exit(1)
        if not isinstance(extra, dict):
            output({"error": "options 需为 JSON 对象"})
            raise typer.Exit(1)
    request(url, "/api/tasks", {"kind": kind, "options": {**extra, "limit": limit, "dry_run": dry_run,
                                                          "round_id": round_id}})


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


def run_task(kind: str, config: str = "config.local.yaml", limit: int = 100, dry_run: bool = False, round_id: str = ""):
    """直接运行一个任务，结束时输出 JSON；与网页使用同一任务核心。

    这个函数同时是 `job-radar run` 这条命令的实现，所以**签名必须保持
    CLI 能生成的形状**（typer 不支持 `dict` 类型的参数，加了会让整个 CLI 起不来）。
    需要更多选项的任务走下面的 `run_task_with_options()`。
    """
    return run_task_with_options(kind, config, limit, dry_run, round_id)


def run_task_with_options(kind: str, config: str = "config.local.yaml", limit: int = 100,
                          dry_run: bool = False, round_id: str = "", options: dict | None = None):
    """`run_task()` 的加选项版本，供需要 phase/pages/keywords 的任务种类（session）使用。

    `options` 只**补充**基础三项：同名键不会覆盖 limit / dry_run / round_id，
    这样「数量上限」和「计划预览」始终只有一个来源。
    """
    payload = {**(options or {}), "limit": limit, "dry_run": dry_run, "round_id": round_id}
    task = None
    try:
        with TaskManager(SettingsStore(config)) as manager:
            task = manager.start(kind, payload)
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
