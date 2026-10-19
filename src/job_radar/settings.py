"""Validated configuration changes with revision checks and local rollback copies."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .candidate import CandidateProfile
from .config import load_config
from .local_files import atomic_write, file_mutex
from .models import CollectionConfig
from .service_config import SECRET_PATHS, public_config

APPLY_POLICY = {
    "llm": "下一批次", "service.batch_size": "下一批次", "request_delay_seconds": "下一批次",
    "detail_delay_seconds": "下一批次", "network_enabled": "下一次请求前检查",
    "browser": "新任务", "refresh": "新任务", "keywords": "新任务", "cities": "新任务", "list_source": "新任务及新采集批次",
    "filters": "新任务", "candidate": "保存后更新岗位复核；采集选行在新任务生效",
    "service.candidate_filtering": "新任务", "service.event_retention": "新任务",
    "service.detail_after_hours": "新任务",
    "database_path": "服务重启",
}


class RevisionConflict(ValueError):
    pass


def resolve_database(config: CollectionConfig, config_path: Path) -> Path:
    value = Path(config.database_path)
    return value.resolve() if value.is_absolute() else (config_path.parent / value).resolve()


def init_config(path: str | Path, database: str = "data/jobs.sqlite3") -> Path:
    target = Path(path).resolve()
    if target.exists():
        raise FileExistsError("配置已存在，不覆盖现有文件")
    cfg = CollectionConfig(cities=[{"name": "全国"}], keywords=["AI Agent", "大模型应用开发", "Python"],
                           database_path=database, pages_per_query=1, detail_limit_per_query=5,
                           candidate=CandidateProfile(roles=["Agent", "智能体", "LLM", "大模型", "RAG"],
                                                      skills=["Python", "SQL", "FastAPI"]))
    with file_mutex(target.with_name(target.name + ".lock")):
        if target.exists():
            raise FileExistsError("配置已存在")
        atomic_write(target, yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
    return target


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        load_config(self.path)

    def read(self) -> tuple[CollectionConfig, str]:
        raw = self.path.read_bytes()
        payload = yaml.safe_load(raw.decode("utf-8-sig"))
        cfg = CollectionConfig.model_validate(payload)
        if cfg.candidate is None:
            cfg.candidate = CandidateProfile()
        return cfg, hashlib.sha256(raw).hexdigest()

    def save(self, payload: dict, expected_revision: str, clear_secrets: list[str] | None = None) -> tuple[CollectionConfig, str]:
        cfg = CollectionConfig.model_validate(payload)
        allowed = {f"{group}.{key}" for group, key in SECRET_PATHS}
        if not isinstance(clear_secrets or [], list) or set(clear_secrets or []) - allowed:
            raise ValueError("密钥清除字段无效")
        with self._lock, file_mutex(self.path.with_name(self.path.name + ".lock")):
            current, revision = self.read()
            if not expected_revision or expected_revision != revision:
                raise RevisionConflict("配置已被其他页面或程序修改，请重新载入后再保存")
            if cfg.database_path != current.database_path:
                raise ValueError("数据库位置在服务启动时确定，修改位置请重启服务")
            if cfg.auto_chat.enabled or not cfg.auto_chat.dry_run:
                raise ValueError("数据服务配置使用关闭的自动联系策略")
            for group, key in SECRET_PATHS:
                target = getattr(cfg, group)
                if f"{group}.{key}" in (clear_secrets or []):
                    setattr(target, key, "")
                elif not getattr(target, key):
                    setattr(target, key, getattr(getattr(current, group), key))
            history = self.path.parent / ".config-history"
            history.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = history / f"{self.path.stem}-{stamp}-{revision[:10]}.yaml"
            atomic_write(backup, yaml.safe_dump(public_config(current), allow_unicode=True, sort_keys=False))
            atomic_write(self.path, yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
            return self.read()
