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
                           max_requests_per_run=30, max_requests_per_day=100,
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

    def save(self, payload: dict, expected_revision: str) -> tuple[CollectionConfig, str]:
        cfg = CollectionConfig.model_validate(payload)
        with self._lock, file_mutex(self.path.with_name(self.path.name + ".lock")):
            current, revision = self.read()
            if not expected_revision or expected_revision != revision:
                raise RevisionConflict("配置已被其他页面或程序修改，请重新载入后再保存")
            if cfg.database_path != current.database_path or cfg.browser != current.browser:
                raise ValueError("工作台不能改变数据库或浏览器路径；请在本机配置文件中修改后重启")
            if cfg.network_enabled or cfg.auto_chat.enabled or not cfg.auto_chat.dry_run:
                raise ValueError("工作台仅保存离线配置；在线访问与真实开聊不能在此启用")
            history = self.path.parent / ".config-history"
            history.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = history / f"{self.path.stem}-{stamp}-{revision[:10]}.yaml"
            atomic_write(backup, self.path.read_text(encoding="utf-8-sig"))
            atomic_write(self.path, yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
            return self.read()
