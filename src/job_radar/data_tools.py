"""Offline setup and consistent SQLite backups."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .analysis.market import connect_readonly
from .local_files import atomic_write


def backup_database(source: str | Path, destination: str | Path) -> dict:
    source, target = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError("源数据库不存在")
    if target.exists():
        raise FileExistsError("备份目标已存在，不覆盖")
    if target == source:
        raise ValueError("备份路径不能与源数据库相同")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing files, including competing backups.
    with target.open("xb"):
        pass
    with closing(connect_readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError("备份未通过完整性检查，保留文件以便诊断")
        count = dst.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"source": str(source), "backup": str(target), "created_at": datetime.now(timezone.utc).isoformat(),
                "jobs": count, "integrity_check": check, "sha256": digest}
    atomic_write(target.with_suffix(target.suffix + ".manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def create_demo(directory: str | Path) -> Path:
    from .candidate import CandidateProfile
    from .models import CollectionConfig, JobRecord, Platform
    from .settings import init_config
    from .storage import JobStore
    import yaml
    root = Path(directory).resolve()
    database = root / "data" / "jobs.sqlite3"
    config = root / "config.local.yaml"
    if database.exists() or config.exists():
        raise FileExistsError("示例目录已含数据库或配置，请选择新目录")
    root.mkdir(parents=True, exist_ok=True)
    init_config(config, "data/jobs.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    with JobStore(database) as store:
        for n, city, title, jd, low, high in [
            (1, "杭州", "示例：Agent应用开发", "【合成示例，非真实招聘】欢迎应届毕业生。使用 Python、FastAPI 构建 Agent 服务，完成 RAG 检索与评测。", 12, 20),
            (2, "上海", "示例：大模型应用工程师", "【合成示例，非真实招聘】有1年 Python 后端经验，使用 SQL 和 RAG 构建知识库。", 15, 25),
            (3, "深圳", "示例：智能体实习生", "【合成示例，非真实招聘】仅限在校生，每周实习4天。", 4, 6),
            (4, "成都", "示例：Python后端工程师", "【合成示例，非真实招聘】使用 Python 和 SQL 开发企业服务，负责 Docker 部署。", 10, 18),
            (5, "北京", "示例：高级Agent工程师", "【合成示例，非真实招聘】必须具备5年以上工程经验，负责 Agent 平台架构。", 30, 50),
            (6, "广州", "示例：待补正文岗位", "", 10, 16),
        ]:
            store.upsert(JobRecord(platform=Platform.BOSS, source_job_id=f"demo-{n}", job_url="", title=title,
                                   company_name=f"合成示例企业{n}", city=city, job_description=jd, salary_text=f"{low}-{high}K",
                                   salary_min_k=low, salary_max_k=high, experience="经验不限", education="本科",
                                   industry="计算机软件", skills=["Python"], matched_queries=["AI Agent", "Python"],
                                   detail_fetched_at=now if jd else ""))
    cfg = CollectionConfig.model_validate(yaml.safe_load(config.read_text(encoding="utf-8")))
    cfg.candidate = CandidateProfile(cities=["杭州", "上海", "深圳"], preferred_cities=["杭州", "上海", "深圳"],
                                     roles=["Agent", "智能体", "大模型"], skills=["Python", "RAG", "FastAPI"])
    atomic_write(config, yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False))
    return config
