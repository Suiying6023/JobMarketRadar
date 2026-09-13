from __future__ import annotations

from pathlib import Path

import yaml

from .models import CollectionConfig


def load_config(path: str | Path) -> CollectionConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(payload, dict):
        raise ValueError("配置的顶层必须是 YAML 映射")
    return CollectionConfig.model_validate(payload)
