"""Collection cohorts, resumable query plans and immutable analysis exports."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from .local_files import atomic_write
from .service_config import public_config
from .storage import JobStore


def stamp():
    return datetime.now(timezone.utc).isoformat()


def rotate_keywords(keywords, recency, limit):
    """按「最久没采过」取前 N 个关键词，而不是永远取配置里的前 N 个。

    ## 为什么需要它

    `max_keywords_per_run=3` 原先的实现是 `config.keywords[:3]` —— 每轮都打**同一批**三个词。
    实测（2026-10-19）：连跑四轮，插入数 154 → 217 → 49 → 31，因为从第二轮起就一直在
    翻同一批结果。配置里 6 个词，后 3 个从来没被采过。

    `recency` 是 `{关键词: 最后一次采集时间}`。**没采过的排最前**（空字符串），
    其次按时间升序；时间相同则保持配置顺序，避免每轮顺序抖动。

    这里只管「选谁」，不碰 `keyword_progress` 的翻页语义 —— 那是另一件事
    （`last_page` / `exhausted` 用于深层续翻）。
    """
    order = {word: index for index, word in enumerate(keywords)}
    ranked = sorted(keywords, key=lambda word: (recency.get(word, ""), order[word]))
    return ranked[:limit]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def query_plan(cfg, recency=None):
    """本轮要打哪些查询。`recency` 给了就按「最久没采过」轮转关键词。

    关键词轮转与页数上限**无条件生效**。历史上它们挂在 `budget_mode == "enforce"`
    分支下，而默认是 `reference`，于是轮转在默认配置下从不发生——那是个 bug，
    不是策略。轮转是采集公平性的要求（每个词都该轮到），与请求预算无关。
    """
    words = rotate_keywords(cfg.keywords, recency or {}, cfg.max_keywords_per_run)
    return [
        {"city": city.model_dump(), "keyword": word, "filters": filters.model_dump(exclude_none=True),
         "pages": min(cfg.pages_for(word), cfg.max_page_number)}
        for city in cfg.cities for word in words for filters in cfg.filters
    ]


def balanced_rows(conn, ident, rows, limit):
    """Round-robin between observed city/query groups for a bounded enrichment task."""
    queries = {row[0]: json.loads(row[1]) for row in conn.execute(
        "SELECT canonical_id,queries_json FROM collection_observations WHERE round_id=?", (ident,))}
    groups = defaultdict(deque)
    for row in rows:
        origin = queries.get(row["canonical_id"], [{}])[0]
        groups[(row["city"], origin.get("keyword", ""))].append(row)
    result = []
    while groups and len(result) < limit:
        for key in sorted(groups, key=lambda value: (value[1], value[0])):
            result.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
            if len(result) == limit:
                break
    return result


class CollectionRound:
    def __init__(self, store: JobStore):
        self.store = store
        self.conn = store.conn
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS collection_rounds (
                round_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL, plan_json TEXT NOT NULL, config_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL, last_task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS collection_queries (
                round_id TEXT NOT NULL REFERENCES collection_rounds(round_id),
                query_key TEXT NOT NULL, completed_at TEXT NOT NULL,
                PRIMARY KEY(round_id, query_key));
            CREATE TABLE IF NOT EXISTS collection_observations (
                round_id TEXT NOT NULL REFERENCES collection_rounds(round_id),
                canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id),
                first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
                queries_json TEXT NOT NULL, row_json TEXT NOT NULL,
                PRIMARY KEY(round_id, canonical_id));
        """)

    @staticmethod
    def fingerprint(cfg):
        # Changing selection rules also changes the sample, even with identical search URLs.
        value = {"plan": query_plan(cfg), "include": cfg.include_title_keywords,
                 "exclude": cfg.exclude_title_keywords, "companies": cfg.exclude_company_keywords,
                 "candidate": cfg.candidate.model_dump(mode="json") if cfg.service.candidate_filtering and cfg.candidate else None}
        if cfg.list_source != "api":
            # Keep legacy API cohort hashes compatible; never mix native page sizes into them.
            value["list_source"] = cfg.list_source
        return hashlib.sha256(encode(value).encode()).hexdigest()

    def begin(self, ident, task_id, cfg):
        plan = query_plan(cfg)
        fingerprint = self.fingerprint(cfg)
        existing = self.conn.execute("SELECT plan_hash FROM collection_rounds WHERE round_id=?", (ident,)).fetchone()
        if existing and existing[0] != fingerprint:
            raise ValueError("该批次的城市、关键词、筛选或页数已改变；请恢复原计划或使用新的 round_id")
        now = stamp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO collection_rounds VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(round_id) DO UPDATE SET updated_at=excluded.updated_at,status='collecting',last_task_id=excluded.last_task_id",
                (ident, now, now, "collecting", encode(plan), encode(public_config(cfg)), fingerprint, task_id))
        return plan

    def require(self, ident):
        if not self.conn.execute("SELECT 1 FROM collection_rounds WHERE round_id=?", (ident,)).fetchone():
            raise ValueError(f"采集批次不存在：{ident}")

    def begin_import(self, ident, plan, provenance, started_at):
        """Start an observed-data cohort without inventing completed query coverage."""
        if self.conn.execute("SELECT 1 FROM collection_rounds WHERE round_id=?", (ident,)).fetchone():
            raise ValueError("导入批次已存在，请指定新的 round_id")
        with self.conn:
            self.conn.execute("INSERT INTO collection_rounds VALUES (?,?,?,?,?,?,?,?)",
                (ident, started_at, stamp(), "importing", encode(plan), encode(provenance),
                 hashlib.sha256(encode(plan).encode()).hexdigest(), "file-import"))

    def completed(self, ident):
        return {row[0] for row in self.conn.execute("SELECT query_key FROM collection_queries WHERE round_id=?", (ident,))}

    def complete_query(self, ident, query):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO collection_queries VALUES (?,?,?)", (ident, encode(query), stamp()))

    def observe(self, ident, canonical_id, query):
        existing = self.conn.execute(
            "SELECT queries_json FROM collection_observations WHERE round_id=? AND canonical_id=?", (ident, canonical_id)).fetchone()
        queries = json.loads(existing[0]) if existing else []
        if query not in queries:
            queries.append(query)
        row = dict(self.conn.execute("SELECT * FROM jobs WHERE canonical_id=?", (canonical_id,)).fetchone())
        now = stamp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO collection_observations VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(round_id,canonical_id) DO UPDATE SET last_observed_at=excluded.last_observed_at,"
                "queries_json=excluded.queries_json,row_json=excluded.row_json",
                (ident, canonical_id, now, now, encode(queries), encode(row)))
        return existing is None

    def update_detail(self, ident, canonical_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE canonical_id=?", (canonical_id,)).fetchone()
        with self.conn:
            self.conn.execute("UPDATE collection_observations SET row_json=? WHERE round_id=? AND canonical_id=?",
                              (encode(dict(row)), ident, canonical_id))

    def count(self, ident):
        return self.conn.execute("SELECT count(*) FROM collection_observations WHERE round_id=?", (ident,)).fetchone()[0]

    def finish(self, ident, status):
        with self.conn:
            self.conn.execute("UPDATE collection_rounds SET status=?,updated_at=? WHERE round_id=?", (status, stamp(), ident))


def list_rounds(database: Path):
    with JobStore(database, read_only=True) as source:
        if not source.conn.execute("SELECT 1 FROM sqlite_master WHERE name='collection_rounds'").fetchone():
            return []
        return [dict(row) for row in source.conn.execute(
            "SELECT r.round_id,r.created_at,r.updated_at,r.status,r.last_task_id,"
            "(SELECT count(*) FROM collection_observations o WHERE o.round_id=r.round_id) AS jobs,"
            "(SELECT count(*) FROM collection_queries q WHERE q.round_id=r.round_id) AS completed_queries "
            "FROM collection_rounds r ORDER BY created_at DESC")]


def export_round(database: Path, ident: str, destination: Path):
    """Export the saved cohort, independent of later changes to the live jobs table."""
    destination = destination.resolve()
    if destination.exists() or destination.with_suffix(".manifest.json").exists():
        raise FileExistsError("快照或清单已存在，请指定新的输出路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with JobStore(database, read_only=True) as source:
        source.conn.execute("BEGIN")
        info = source.conn.execute("SELECT * FROM collection_rounds WHERE round_id=?", (ident,)).fetchone()
        if info is None:
            raise ValueError(f"采集批次不存在：{ident}")
        rows = [json.loads(row[0]) for row in source.conn.execute(
            "SELECT row_json FROM collection_observations WHERE round_id=? ORDER BY canonical_id", (ident,))]
        if not rows:
            raise ValueError("批次没有岗位，不导出空快照")
        completed = source.conn.execute("SELECT count(*) FROM collection_queries WHERE round_id=?", (ident,)).fetchone()[0]
        evidence_tables = {
            "collection_rounds": [dict(info)],
            "collection_queries": [dict(row) for row in source.conn.execute(
                "SELECT * FROM collection_queries WHERE round_id=?", (ident,))],
            "collection_observations": [dict(row) for row in source.conn.execute(
                "SELECT * FROM collection_observations WHERE round_id=?", (ident,))],
        }
        for name in ("job_annotations", "job_status_checks"):
            if source.conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone():
                evidence_tables[name] = [dict(row) for row in source.conn.execute(
                    f"SELECT * FROM {name} WHERE canonical_id IN "
                    "(SELECT canonical_id FROM collection_observations WHERE round_id=?)", (ident,))]
        if evidence_tables.get("job_annotations"):
            evidence_tables["annotation_prompts"] = [dict(row) for row in source.conn.execute(
                "SELECT * FROM annotation_prompts WHERE prompt_hash IN "
                "(SELECT prompt_hash FROM job_annotations WHERE canonical_id IN "
                "(SELECT canonical_id FROM collection_observations WHERE round_id=?))", (ident,))]
    # Build into a temporary sibling so a failed export never appears as a finished snapshot.
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"存在未完成的快照：{temporary}")
    with JobStore(temporary) as target:
        CollectionRound(target)
        if evidence_tables:
            from .annotations import ensure_schema
            ensure_schema(target.conn)
        columns = [row[1] for row in target.conn.execute("PRAGMA table_info(jobs)")]
        target.conn.executemany(
            f"INSERT INTO jobs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows])
        for name, records in evidence_tables.items():
            if not records:
                continue
            fields = list(records[0])
            target.conn.executemany(f"INSERT INTO {name} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                    [tuple(row[field] for field in fields) for row in records])
        target.conn.commit()
        target.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.conn.execute("PRAGMA journal_mode=DELETE")
    temporary.rename(destination)
    manifest = {"method": "saved collection_observations row snapshots", "round_id": ident,
                "source": str(database.resolve()), "created_at_utc": stamp(), "jobs": len(rows),
                "nonempty_jd": sum(bool(row["job_description"].strip()) for row in rows),
                "completed_queries": completed, "round": dict(info),
                "evidence_rows": {name: len(records) for name, records in evidence_tables.items()},
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "interpretation": "本轮关键词检索样本；未出现的旧岗位不能据此判为关闭。正文时效需核对 detail_fetched_at。"}
    def has_verified_body(row):
        raw = json.loads(row.get("raw_json") or "{}")
        return bool(row["job_description"].strip()) and raw.get("detail_access_verified") is True
    def fetched_in_round(row):
        try:
            fetched = datetime.fromisoformat(row["detail_fetched_at"].replace("Z", "+00:00"))
            started = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return started <= fetched <= datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False
    manifest["body_quality"] = {
        "verified_fetched_in_round": sum(has_verified_body(row) and fetched_in_round(row) for row in rows),
        "verified_inherited_or_time_unconfirmed": sum(has_verified_body(row) and not fetched_in_round(row) for row in rows),
        "nonempty_access_unconfirmed": sum(bool(row["job_description"].strip()) and not has_verified_body(row) for row in rows),
        "missing": sum(not row["job_description"].strip() for row in rows),
    }
    manifest["round"]["plan"] = json.loads(manifest["round"].pop("plan_json"))
    config_info = json.loads(manifest["round"]["config_json"])
    if config_info.get("mode") == "offline-import":
        manifest["capture_provenance"] = config_info
    manifest["round"].pop("config_json")
    atomic_write(destination.with_suffix(".manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest
