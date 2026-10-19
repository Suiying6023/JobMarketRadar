"""Owned worker process. All task control is through the durable service store."""
from __future__ import annotations

import argparse
import asyncio
import os
import time

from .safety import AccessGate
from .service_config import redact
from .settings import SettingsStore, resolve_database
from .task_store import TaskStore
from .annotations import hash_json

HOT_FIELDS = ("llm", "network_enabled", "request_delay_seconds", "detail_delay_seconds", "detail_batch",
              "detail_cooldown_seconds", "burst_threshold", "burst_penalty_seconds")


def changed_fields(actual, requested, prefix=""):
    if isinstance(actual, dict) and isinstance(requested, dict):
        result = []
        for key in actual.keys() | requested.keys():
            result.extend(changed_fields(actual.get(key), requested.get(key), f"{prefix}.{key}".strip(".")))
        return result
    return [prefix] if actual != requested else []


class TaskContext:
    def __init__(self, config, store, ident):
        self.settings = SettingsStore(config)
        self.store = store
        self.ident = ident
        self.task = store.get(ident)
        self.initial, self.revision = self.settings.read()
        self._observed_revision = self.revision
        self._effective_hash = hash_json(self.initial.model_dump(mode="json"))
        self.store.retention = self.initial.service.event_retention
        self.database = resolve_database(self.initial, self.settings.path)
        self.progress = {"processed": 0, "total": 0, "failed": 0}

    def config(self):
        cfg, revision = self.settings.read()
        if resolve_database(cfg, self.settings.path) != self.database:
            raise RuntimeError("数据库位置已改变，请重新启动服务")
        effective = self.initial.model_copy(deep=True)
        for field in HOT_FIELDS:
            setattr(effective, field, getattr(cfg, field))
        effective.service.batch_size = cfg.service.batch_size
        if revision != self._observed_revision:
            pending = sorted(changed_fields(effective.model_dump(mode="json"), cfg.model_dump(mode="json")))
            fingerprint = hash_json(effective.model_dump(mode="json"))
            if not pending:
                self.revision = revision
            elif fingerprint != self._effective_hash:
                self.revision = "partial:" + fingerprint
            self._effective_hash = fingerprint
            self._observed_revision = revision
            self.progress["pending_configuration"] = {"revision": revision, "fields": pending} if pending else None
            self.store.update(self.ident, applied_revision=self.revision, progress=self.progress)
            self.log("批次配置已更新" if not pending else "批次配置已更新；以下字段在新任务生效：" + "、".join(pending),
                     kind="config", requested_revision=revision, applied_revision=self.revision, pending_fields=pending)
        return effective

    def require_access(self):
        cfg, _ = self.settings.read()
        AccessGate(self.database).require_online(cfg.network_enabled)
        if self.task["kind"] == "refresh":
            gate = AccessGate(self.database, scope="refresh")
            if gate.state_path.exists():
                gate.require_online(cfg.network_enabled)

    def log(self, message, *, level="info", kind="log", **data):
        cfg, _ = self.settings.read()
        self.store.event(self.ident, kind, redact(str(message), cfg), level=level, data=data)

    def report(self, **values):
        self.progress.update(values)
        self.store.update(self.ident, progress=self.progress)

    async def verified(self, operation, browser, page):
        from .collectors.boss import PlatformRiskStop
        while True:
            try:
                return await operation()
            except PlatformRiskStop as exc:
                message = redact(str(exc), self.config())
                self.log(message, level="warning", kind="risk", category=exc.category,
                         code=exc.code, http_status=exc.http_status)
                # The persistent hold prevents another automated task from proceeding.
                owner = f"managed-task:{self.ident}"
                gate = AccessGate(self.database, scope="refresh" if self.task["kind"] == "refresh" else "")
                try:
                    state = gate.pause(message, source=owner)
                except Exception:
                    self.log("平台风险已记录，暂停状态写入失败；任务停止", level="error", kind="risk")
                    raise
                if state.source != owner:
                    raise RuntimeError("已有独立的访问暂停，当前任务停止并保留该状态")
                # Refresh can fail on a document fetched in the background, while
                # the browser still displays its initial job. Point the operator
                # to the failed public job URL, rather than that unrelated tab URL.
                url = exc.request_url or getattr(page, "url", "") or ""
                if not url.startswith("https://www.zhipin.com/"):
                    url = ""
                self.store.update(self.ident, status="waiting_verification", verification={
                    "url": url.split("?")[0], "message": message, "category": exc.category,
                    "code": exc.code, "http_status": exc.http_status})
                self.log("任务已暂停，等待平台状态和当前业务请求复检", kind="verification", category=exc.category)
                # 自动把窗口推到前台。实测（2026-10-18）`code=36` 是"要求人工过验证"，
                # 不是限速：等 12 分钟无效，整页重载也无效，只有人点一下能解。
                # 原先必须由操作者再敲一条 show 命令才会弹窗，等于把一个"必须人来"
                # 的动作藏在了第二道门后面。
                try:
                    if page is not None and not getattr(self.config().browser, "headless", False):
                        await browser.show(page)
                        on_verify = any(hint in (getattr(page, "url", "") or "")
                                        for hint in ("verify", "safe-validate"))
                        self.log("已自动弹出浏览器窗口" +
                                 ("；当前就是验证页，请直接完成验证" if on_verify
                                  else "；若不是验证页，请人工确认账号状态"),
                                 kind="verification", category=exc.category)
                except Exception:  # noqa: BLE001 - 弹窗失败不影响暂停语义
                    self.log("窗口显示失败，请检查浏览器是否可见后重试", level="warning")
                while True:
                    if self.store.consume(self.ident, "show"):
                        try:
                            await browser.show(page)
                            self.log("验证窗口已显示")
                        except Exception:
                            self.log("窗口显示失败，请检查浏览器是否可见后重试", level="warning")
                    if self.store.consume(self.ident, "resume"):
                        self.require_access()
                        self.store.update(self.ident, status="running", verification=None)
                        self.log("复检当前业务请求")
                        break
                    await asyncio.sleep(0.25)


async def run(context, parent):
    from .service_jobs import execute
    current = asyncio.current_task()

    async def watch():
        while True:
            commands = context.store.commands(context.ident)
            if commands.get("stop_requested") or (os.name != "nt" and os.getppid() != parent):
                current.cancel()
                return
            await asyncio.sleep(0.2)

    guard = asyncio.create_task(watch())
    try:
        result = await execute(context)
        failed = int(result.get("failed", context.progress.get("failed", 0)) or 0)
        if failed:
            message = f"任务处理结束，仍有{failed}条记录失败；已保存的成功结果保留，可重新处理失败项"
            context.log(message, level="error")
            context.store.finish(context.ident, "failed", result=result, error=message)
        else:
            context.store.finish(context.ident, "succeeded", result=result)
    except asyncio.CancelledError:
        context.store.finish(context.ident, "cancelled")
    except Exception as exc:
        try:
            cfg, _ = context.settings.read()
            error = redact(redact(f"{type(exc).__name__}: {exc}", context.initial), cfg)
        except Exception:
            error = f"{type(exc).__name__}：任务配置读取失败"
        context.log(error, level="error")
        context.store.finish(context.ident, "failed", error=error[:1000])
    finally:
        guard.cancel()
        try:
            await guard
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--parent", required=True, type=int)
    args = parser.parse_args()
    store = TaskStore(args.store)
    # The parent registers OS process ownership before allowing browser/model work.
    deadline = time.monotonic() + 15
    while store.get(args.task)["status"] == "queued":
        if time.monotonic() > deadline or store.commands(args.task).get("stop_requested"):
            return
        time.sleep(0.05)
    if store.get(args.task)["status"] != "running":
        return
    asyncio.run(run(TaskContext(args.config, store, args.task), args.parent))


if __name__ == "__main__":
    main()
