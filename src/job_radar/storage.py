from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import profile as P
from .models import JobRecord
from .normalization import merge_unique, normalized_text, stable_id


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    canonical_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    job_url TEXT NOT NULL,
    company_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    business_district TEXT NOT NULL DEFAULT '',
    location_text TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    salary_min_k REAL,
    salary_max_k REAL,
    salary_months INTEGER,
    experience TEXT NOT NULL DEFAULT '',
    education TEXT NOT NULL DEFAULT '',
    company_size TEXT NOT NULL DEFAULT '',
    financing_stage TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    recruiter_name TEXT NOT NULL DEFAULT '',
    recruiter_title TEXT NOT NULL DEFAULT '',
    recruiter_active_status TEXT NOT NULL DEFAULT '',
    recruiter_active_days INTEGER,
    recruiter_online INTEGER NOT NULL DEFAULT 0,
    is_proxy_job INTEGER NOT NULL DEFAULT 0,
    job_invalid INTEGER NOT NULL DEFAULT 0,
    address TEXT NOT NULL DEFAULT '',
    job_description TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    labels_json TEXT NOT NULL DEFAULT '[]',
    welfare_json TEXT NOT NULL DEFAULT '[]',
    matched_queries_json TEXT NOT NULL DEFAULT '[]',
    source_rank INTEGER,
    -- 详情接口里已经抓到、原先没落库的字段。
    brand_id TEXT NOT NULL DEFAULT '',
    recruiter_id TEXT NOT NULL DEFAULT '',
    recruiter_certificated INTEGER NOT NULL DEFAULT 0,
    company_intro TEXT NOT NULL DEFAULT '',
    company_founded_year INTEGER,
    job_status_desc TEXT NOT NULL DEFAULT '',
    position_name TEXT NOT NULL DEFAULT '',
    recruitment_count TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL,
    -- 增量采集的时间轴：first_seen_at 永不覆盖，用来算岗位存活天数。
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    detail_fetched_at TEXT NOT NULL DEFAULT '',
    -- 分层保留：active=画像内投递池；sample=画像外但保留作增量去重样本；
    -- purged=待物理清理。状态只由 clean/bookmark 脚本管理，采集 upsert 不覆盖。
    retention_status TEXT NOT NULL DEFAULT 'active',
    -- 用户浏览/投递过的岗位，画像外也保留（投递池 = active OR bookmarked）。
    bookmarked INTEGER NOT NULL DEFAULT 0,
    -- LLM 从 JD 提取的结构化标签（exp_req/edu_req/school_req/tech_req/other_req/bonus/job_type）。
    glm_tags_json TEXT NOT NULL DEFAULT '{}',
    glm_tags_updated_at TEXT NOT NULL DEFAULT '',
    -- 工商登记信息（gongsi 页免登录抓取，按 brand_id 富化）：
    -- 注册资本/成立时间/法定代表人/企业类型/经营状态/统一社会信用代码/注册地址等。
    company_reg_json TEXT NOT NULL DEFAULT '{}',
    company_reg_updated_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, source_job_id)
);

-- 每次采集运行的记录，用来对比多轮之间的增量（新增了多少、请求用了多少）。
-- 岗位第一次出现的时间已经在 jobs.first_seen_at 里，不需要再建一张按运行记录
-- 出现次数的表——那张表没有代码会写入，属于当前不需要的实体。
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_label TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}'
);

-- 请求账本（2026-08-10 加）。封禁是**累计量制**（同 IP 累计请求量 + 账号信任度），
-- 不是频率制——所以「单日 ≤N 次」这条安全线必须跨进程累计才有意义。
-- 原先只有 collection_runs 表且 record_run() 零调用，实查 0 行，等于没有账本：
-- expand_list / backfill_details / backfill_activity 各跑各的，谁也不知道今天
-- 一共发了多少请求，"今天已经很克制了"完全是凭感觉。
-- 一行 = 一次真实 HTTP 请求，kind 区分 list/detail/security_check（三者风险不同）。
-- ⚠️ 两条链路都写这张表，但**各有独立日线**：采集链路（list/detail/security_check，
-- 带账号 cookie + 家宽 IP）由 `max_requests_per_day` 卡，口径见
-- `JobStore.COLLECT_KINDS`；状态刷新链路（detail_page，免登录 + 波尔机房 IP）
-- 由 refresh_status_loop 自己的 `--daily-cap` 卡。风控按「IP + 账号信任度」判，
-- 两条链路既不同出口也不同登录态，合并计数只会让彼此凭空少掉额度。
-- ⚠️ ts 由 Python 写入**本地时间**，不用 SQLite 的 CURRENT_TIMESTAMP（那是 UTC）。
-- 混用会让同一行的 ts 和 day 差 8 小时（day 按本地日算，见 log_request 注释），
-- 看账本时极易误判成两批不同的事件——实测已经误导过一次判读。
-- 落盘的 risk_dump 文件名也是本地时间，统一本地时间才能对齐着看。
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'ok',
    code INTEGER,
    script TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

-- 关键词翻页进度（2026-08-11 加）。记住每个 (城市, 关键词) 翻到第几页。
--
-- 为什么必需：`pages_per_query` 原本是每个关键词的**永久天花板**，不是单轮上限。
-- 因为 `--dry-stop` 靠"连续 N 页零新增"提前收尾，而第二轮跑同一个词时前几页
-- 必然全是已见岗位 → 立刻触发 dry-stop 跳过 → 页 N+1 以后**永远到不了**。
-- 实测：`向量数据库` 一直卡在 150 条（=5页×30），换 10 页后页4→14、页5→23、
-- 页6→24、页7→25 条新增，越深越出货，说明它从来不是没货而是被截断。
-- 有了这张表就能"接着上次翻"，把深层矿脉挖完。
CREATE TABLE IF NOT EXISTS keyword_progress (
    city TEXT NOT NULL,
    keyword TEXT NOT NULL,
    last_page INTEGER NOT NULL DEFAULT 0,   -- 已翻到第几页
    exhausted INTEGER NOT NULL DEFAULT 0,   -- 1 = 确认翻到底（返回空 jobList）
    last_new INTEGER NOT NULL DEFAULT 0,    -- 上次那页新增几条（判断是否还值得翻）
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (city, keyword)
);

CREATE TABLE IF NOT EXISTS cross_platform_groups (
    group_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id) ON DELETE CASCADE,
    confidence TEXT NOT NULL DEFAULT 'candidate',
    PRIMARY KEY(group_id, canonical_id)
);

CREATE TABLE IF NOT EXISTS chat_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id),
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# 索引单独放一份，必须在列迁移之后才能建——建表脚本对旧库是 IF NOT EXISTS
# 空操作，这时新字段还不存在，索引若和建表脚本放在一起执行会直接报错。
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_name);
CREATE INDEX IF NOT EXISTS idx_jobs_brand ON jobs(brand_id);
CREATE INDEX IF NOT EXISTS idx_jobs_recruiter ON jobs(recruiter_id);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_retention ON jobs(retention_status);
CREATE INDEX IF NOT EXISTS idx_request_log_day ON request_log(day, kind);
"""


class JobStore:
    # 列名 -> (SQLite 类型声明, 用于 ALTER TABLE 的默认值表达式)。
    # CREATE TABLE IF NOT EXISTS 不会给已存在的旧库补列，所以老库（比如上次
    # 只有 37 列那版）打开时要靠这张表逐个 ALTER TABLE ADD COLUMN 补齐。
    MIGRATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("recruiter_certificated", "INTEGER NOT NULL DEFAULT 0", "0"),
        ("brand_id", "TEXT NOT NULL DEFAULT ''", "''"),
        ("recruiter_id", "TEXT NOT NULL DEFAULT ''", "''"),
        ("company_intro", "TEXT NOT NULL DEFAULT ''", "''"),
        ("company_founded_year", "INTEGER", "NULL"),
        ("job_status_desc", "TEXT NOT NULL DEFAULT ''", "''"),
        ("position_name", "TEXT NOT NULL DEFAULT ''", "''"),
        ("recruitment_count", "TEXT NOT NULL DEFAULT ''", "''"),
        ("latitude", "REAL", "NULL"),
        ("longitude", "REAL", "NULL"),
        # first_seen_at/last_seen_at 在建表语句里是 NOT NULL 无默认值，旧库
        # 补列时用已有的 collected_at 回填，保证语义一致（首次见到的时间）。
        ("first_seen_at", "TEXT NOT NULL DEFAULT ''", "collected_at"),
        ("last_seen_at", "TEXT NOT NULL DEFAULT ''", "collected_at"),
        ("seen_count", "INTEGER NOT NULL DEFAULT 1", "1"),
        ("detail_fetched_at", "TEXT NOT NULL DEFAULT ''", "''"),
        ("retention_status", "TEXT NOT NULL DEFAULT 'active'", "'active'"),
        ("bookmarked", "INTEGER NOT NULL DEFAULT 0", "0"),
        ("jd_tags_json", "TEXT NOT NULL DEFAULT '{}'", "'{}'"),
        ("glm_tags_json", "TEXT NOT NULL DEFAULT '{}'", "'{}'"),
        ("glm_tags_updated_at", "TEXT NOT NULL DEFAULT ''", "''"),
        ("company_reg_json", "TEXT NOT NULL DEFAULT '{}'", "'{}'"),
        ("company_reg_updated_at", "TEXT NOT NULL DEFAULT ''", "''"),
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout=30：采集（补详情）常与刷新链路、标签重打并行跑，
        # 三方写同一个库。库是 WAL（写者排队而非直接失败），但默认 5s 的
        # busy_timeout 在三方争抢下会抛 "database is locked" ——
        # 而这一侧写的是**花了不可退配额换来的详情**，崩在写库最不值。
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_legacy_columns()
        self.conn.executescript(SCHEMA_INDEXES)

    def _migrate_legacy_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for name, decl, backfill in self.MIGRATION_COLUMNS:
            if name in existing:
                continue
            self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            if backfill == "collected_at":
                # 回填用 collected_at，把旧数据的“首次/最近见到”都视作上次采集时间。
                self.conn.execute(f"UPDATE jobs SET {name} = collected_at")
            elif backfill != "NULL":
                self.conn.execute(f"UPDATE jobs SET {name} = {backfill}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def cross_platform_group(job: JobRecord) -> str:
        return stable_id(job.company_name, job.title, job.city, length=20)

    # 增量采集：first_seen_at 只在首次插入时写入，之后永不覆盖；
    # last_seen_at 每次命中都刷新；seen_count 累加。三者合起来才能回答
    # “这个岗位第一次出现是什么时候、最近还在不在、一共被看到过几次”。
    INSERT_COLUMNS = (
        "canonical_id", "platform", "source_job_id", "job_url", "company_url",
        "title", "company_name", "city", "district", "business_district",
        "location_text", "salary_text", "salary_min_k", "salary_max_k",
        "salary_months", "experience", "education", "company_size",
        "financing_stage", "industry", "recruiter_name", "recruiter_title",
        "recruiter_active_status", "recruiter_active_days", "recruiter_online",
        "recruiter_certificated", "recruiter_id", "brand_id", "company_intro",
        "company_founded_year", "job_status_desc", "position_name",
        "recruitment_count", "latitude", "longitude",
        "is_proxy_job", "job_invalid", "address", "job_description", "detail_fetched_at",
        "skills_json", "labels_json", "welfare_json", "matched_queries_json",
        "source_rank", "raw_json", "collected_at",
        "first_seen_at", "last_seen_at",
        "retention_status", "bookmarked",
    )

    # 重新采集时这些字段直接用新值覆盖（平台上的最新状态）。
    # ⚠️ raw_json 绝不能加进这个列表：列表轮次的 raw 只有列表字段，无条件覆盖会把
    # 含 detail 的完整 raw 连同新鲜 securityId/lid 一起抹掉（曾丢掉 34% 的
    # raw.detail，且被列表重复命中越多丢得越快）。它走 Python 侧 merge，见 upsert。
    OVERWRITE_COLUMNS = (
        "job_url", "company_url", "title", "company_name", "city", "district",
        "business_district", "location_text", "salary_text", "salary_min_k",
        "salary_max_k", "salary_months", "experience", "education",
        "company_size", "financing_stage", "industry", "recruiter_name",
        "recruiter_title", "source_rank", "collected_at",
        # recruiter_online 留在这里是对的：列表接口确实返回 bossOnline，
        # 且"当前是否在线"本就是会来回变的实时状态，不该做单向保护。
        "recruiter_online",
    )

    # 只在新值非空时覆盖。判据很简单：**凡是只有详情接口返回的字段都必须在这里**，
    # 否则每跑一次列表采集，就会用列表轮次的空值把花详情配额换来的数据清空一批。
    # 加新字段时先确认 `_parse_api_job` 是否给它赋值——不赋值的就属于这一类。
    # 抹除率按 seen_count 单调爬升可以验证是否漏了字段：同一岗位被列表扫到越多次，
    # 漏掉的列被清空的比例越高（曾出现 seen≥8 的行 85% 被抹）。
    # recruiter_online 不在此列：列表确实返回 bossOnline，且"当前在线"是会来回变的
    # 实时状态，做单向保护反而会把旧的 True 永久钉住。
    KEEP_IF_EMPTY_COLUMNS = (
        "recruiter_id", "brand_id", "company_intro", "company_founded_year",
        "address", "latitude", "longitude", "detail_fetched_at",
        "recruiter_active_status",
        "job_status_desc", "position_name", "recruitment_count",
    )

    # 只在新值为 NULL 时保留旧值。`recruiter_active_days = 0` 是**合法且最强**的
    # 活跃信号（今天活跃过），绝不能当成"空值"跳过——所以它不能和下面的布尔标记
    # 共用「0 也算空」的规则。
    KEEP_IF_NULL_COLUMNS = (
        "recruiter_active_days",
    )

    # 单向布尔标记：只有详情接口给，且一旦为真就不该被列表轮次的 0 覆盖回假。
    # （岗位下架、代招不会变回来；真要重置由 clean/backfill 脚本直接 UPDATE。）
    KEEP_IF_FALSY_COLUMNS = (
        "recruiter_certificated",    # 招聘者认证
        "is_proxy_job",              # 代招标记
        "job_invalid",               # 岗位失效
    )

    def upsert(self, job: JobRecord) -> str:
        canonical_id = job.canonical_key()
        existing = self.conn.execute(
            "SELECT matched_queries_json, skills_json, welfare_json, raw_json FROM jobs WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()

        payload = job.model_dump(mode="json")

        if existing:
            # 多关键词命中同一岗位时合并关键词，不是覆盖。
            job.matched_queries = merge_unique(
                json.loads(existing["matched_queries_json"] or "[]"), job.matched_queries
            )
            job.skills = merge_unique(json.loads(existing["skills_json"] or "[]"), job.skills)
            job.welfare = merge_unique(json.loads(existing["welfare_json"] or "[]"), job.welfare)
            # raw_json merge：列表路径的新 raw 覆盖顶层字段，但保留旧 raw 里的
            # detail / securityId_refreshed 等只有详情路径才能写进去的键。
            # 若不 merge，下一轮列表采集会用「纯列表 raw」覆盖「含 detail 的完整 raw」，
            # 把 raw.detail 和新鲜的 securityId/lid 全部抹掉。
            try:
                old_raw = json.loads(existing["raw_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                old_raw = {}
            if old_raw:
                new_raw = payload.get("raw", {})
                merged_raw = {**old_raw, **new_raw}  # 新值覆盖同名顶层键
                payload["raw"] = merged_raw
            status = "updated"
        else:
            status = "inserted"
        # ⚠️ 不能用 payload["collected_at"]（记录自带的时间戳）。
        # backfill 脚本从 DB 读回旧行补详情时，会把**旧的** collected_at 一起带回来
        # （backfill_details.py 的 _row_to_job），于是 last_seen_at 被往回写成几天前——
        # 2026-08-11 实测 144/235 行的 last_seen_at 早于 detail_fetched_at，
        # "最近一次见到"这个语义直接失真，导出层算活跃度年龄时基准是错的。
        # last_seen_at/collected_at 语义都是"这次操作发生在何时"，必须取当下。
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        params = {
            "canonical_id": canonical_id,
            "platform": job.platform.value,
            "source_job_id": job.source_job_id,
            "job_url": job.job_url,
            "company_url": job.company_url,
            "title": job.title,
            "company_name": job.company_name,
            "city": job.city,
            "district": job.district,
            "business_district": job.business_district,
            "location_text": job.location_text,
            "salary_text": job.salary_text,
            "salary_min_k": job.salary_min_k,
            "salary_max_k": job.salary_max_k,
            "salary_months": job.salary_months,
            "experience": job.experience,
            "education": job.education,
            "company_size": job.company_size,
            "financing_stage": job.financing_stage,
            "industry": job.industry,
            "recruiter_name": job.recruiter_name,
            "recruiter_title": job.recruiter_title,
            "recruiter_active_status": job.recruiter_active_status,
            "recruiter_active_days": job.recruiter_active_days,
            "recruiter_online": int(job.recruiter_online),
            "recruiter_certificated": int(job.recruiter_certificated),
            "recruiter_id": job.recruiter_id,
            "brand_id": job.brand_id,
            "company_intro": job.company_intro,
            "company_founded_year": job.company_founded_year,
            "job_status_desc": job.job_status_desc,
            "position_name": job.position_name,
            "recruitment_count": job.recruitment_count,
            "latitude": job.latitude,
            "longitude": job.longitude,
            "is_proxy_job": int(job.is_proxy_job),
            "job_invalid": int(job.job_invalid),
            "address": job.address,
            "job_description": job.job_description,
            "detail_fetched_at": job.detail_fetched_at,
            "skills_json": json.dumps(job.skills, ensure_ascii=False),
            "labels_json": json.dumps(job.labels, ensure_ascii=False),
            "welfare_json": json.dumps(job.welfare, ensure_ascii=False),
            "matched_queries_json": json.dumps(job.matched_queries, ensure_ascii=False),
            "source_rank": job.source_rank,
            "raw_json": json.dumps(payload.get("raw", {}), ensure_ascii=False),
            "retention_status": job.retention_status,
            "bookmarked": int(job.bookmarked),
            "collected_at": now,
            "first_seen_at": now,
            "last_seen_at": now,
        }

        columns = ", ".join(self.INSERT_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in self.INSERT_COLUMNS)
        updates = [f"{name}=excluded.{name}" for name in self.OVERWRITE_COLUMNS]
        # 新值为空时保留旧值，防止只抓列表的轮次覆盖掉详情数据。
        updates += [
            f"{name}=CASE WHEN excluded.{name} IS NULL OR excluded.{name} = '' "
            f"THEN jobs.{name} ELSE excluded.{name} END"
            for name in self.KEEP_IF_EMPTY_COLUMNS
        ]
        # 只在新值为 NULL 时保留旧值（0 是合法值，必须写进去）。
        updates += [
            f"{name}=CASE WHEN excluded.{name} IS NULL "
            f"THEN jobs.{name} ELSE excluded.{name} END"
            for name in self.KEEP_IF_NULL_COLUMNS
        ]
        # 单向布尔标记：列表轮次的 0 不把详情抓到的真值覆盖回假。
        updates += [
            f"{name}=CASE WHEN excluded.{name} IS NULL OR excluded.{name} = 0 "
            f"THEN jobs.{name} ELSE excluded.{name} END"
            for name in self.KEEP_IF_FALSY_COLUMNS
        ]
        # JD 保留更长的那份：第二轮若只抓列表，不该把已有 JD 清空。
        updates.append(
            "job_description=CASE WHEN length(excluded.job_description) > "
            "length(jobs.job_description) THEN excluded.job_description "
            "ELSE jobs.job_description END"
        )
        updates += [
            "skills_json=excluded.skills_json",
            "labels_json=excluded.labels_json",
            "welfare_json=excluded.welfare_json",
            "matched_queries_json=excluded.matched_queries_json",
            # raw_json 的 excluded 值已在 Python 侧与旧 raw merge 过（保留 detail 等
            # 只有详情路径能写的键），所以这里可以安全覆盖。
            "raw_json=excluded.raw_json",
            # first_seen_at 刻意不在更新列表里，保证首次发现时间永不丢失。
            "last_seen_at=excluded.last_seen_at",
            "seen_count=jobs.seen_count + 1",
            "updated_at=CURRENT_TIMESTAMP",
        ]

        self.conn.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(canonical_id) DO UPDATE SET {', '.join(updates)}",
            params,
        )
        group_id = self.cross_platform_group(job)
        self.conn.execute(
            "INSERT OR IGNORE INTO cross_platform_groups(group_id, canonical_id) VALUES (?, ?)",
            (group_id, canonical_id),
        )
        self.conn.commit()
        return status

    def record_run(self, config_label: str, stats_json: str) -> int:
        """记录一次采集运行，便于对比多轮之间的增量。"""
        cursor = self.conn.execute(
            "INSERT INTO collection_runs(config_label, stats_json) VALUES (?, ?)",
            (config_label, stats_json),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    # ---- 请求账本 ----
    # 单独 commit（不跟 upsert 混）：账本的意义是"请求已经发出去了"，即使后续写库
    # 失败或整个进程崩掉，那次请求也真实消耗了配额，账不能回滚。
    def log_request(
        self,
        kind: str,
        outcome: str = "ok",
        code: int | None = None,
        script: str = "",
        note: str = "",
    ) -> None:
        """记一次真实 HTTP 请求。kind: list / detail / security_check。

        `day` 用**本地日历日**而不是 UTC：封禁的自动解锁时间是按平台所在时区
        （北京时间）给的，用 UTC 会让"今天"在早上 8 点前错位到前一天，
        把昨天的额度算进今天。
        """
        now = datetime.now().astimezone()
        self.conn.execute(
            "INSERT INTO request_log(ts, day, kind, outcome, code, script, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now.strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d"),
                kind,
                outcome,
                code,
                script,
                note,
            ),
        )
        self.conn.commit()

    #: 采集链路的 kind（带账号 cookie、走本机家宽、打 /wapi JSON 接口）。
    #: `total` 只累计这些——它是 `max_requests_per_day` 的执行口径。
    COLLECT_KINDS = frozenset({"list", "detail", "security_check", "recommend"})

    def request_counts(self, day: str | None = None) -> dict[str, int]:
        """某天各类请求数（默认今天本地日）。返回 {'list': n, ..., 'total': n}。

        `total` **只累计采集链路**（`COLLECT_KINDS`），不含状态刷新链路的
        `detail_page`。两条链路是不同账号（带 cookie vs 免登录）、不同出口
        （家宽 vs 波尔机房 IP），风控是按「IP + 账号信任度」判的，各自独立记账——
        把免登录跳板的请求算进采集账号的日线，会让采集侧凭空少掉额度：
        刷 100 条状态（1400 次 detail_page）就能把日线 1000 直接顶满，
        而那 1400 次请求从未经过采集账号或家宽 IP。

        刷新链路自己按 `--daily-cap` 卡 `detail_page` 的量（见
        `scripts/refresh_status_loop.py`），不共用这条线。
        """
        day = day or datetime.now().astimezone().strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) FROM request_log WHERE day = ? GROUP BY kind", (day,)
        ).fetchall()
        counts = {str(k): int(n) for k, n in rows}
        counts["total"] = sum(
            n for kind, n in counts.items() if kind in self.COLLECT_KINDS
        )
        return counts

    # ---- 关键词翻页进度 ----
    def get_keyword_progress(self, city: str, keyword: str) -> tuple[int, bool]:
        """返回 (已翻到第几页, 是否确认翻到底)。没记录则 (0, False)。"""
        row = self.conn.execute(
            "SELECT last_page, exhausted FROM keyword_progress "
            "WHERE city = ? AND keyword = ?", (city, keyword)
        ).fetchone()
        if not row:
            return 0, False
        return int(row[0]), bool(row[1])

    def set_keyword_progress(self, city: str, keyword: str, last_page: int,
                             exhausted: bool = False, last_new: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO keyword_progress(city, keyword, last_page, exhausted, "
            "  last_new, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city, keyword) DO UPDATE SET "
            "  last_page = excluded.last_page, exhausted = excluded.exhausted, "
            "  last_new = excluded.last_new, updated_at = excluded.updated_at",
            (city, keyword, last_page, int(exhausted), last_new,
             datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()

    def record_chat_action(self, canonical_id: str, action: str, status: str, message: str = "") -> None:
        self.conn.execute(
            "INSERT INTO chat_actions(canonical_id, action, status, message) VALUES (?, ?, ?, ?)",
            (canonical_id, action, status, message),
        )
        self.conn.commit()

    def detailed_ids(self) -> set[str]:
        """已抓到 JD 的 canonical_id 集合，供采集器在调详情接口前跳过。

        详情接口是整条链路里最贵的资源（约 4-5 条/cookie 窗口，封禁红线 150-200 条），
        原先列表页每见到一个画像内岗位就无条件抓详情，同一岗位在多个关键词的搜索
        结果里重复出现时会被反复抓取 —— 库里 13 个关键词交叉覆盖，重复率很高。
        """
        return {
            r[0] for r in self.conn.execute(
                "SELECT canonical_id FROM jobs WHERE job_description IS NOT NULL "
                "AND job_description != ''"
            )
        }

    def rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM jobs ORDER BY city, company_name, title"))

    def export_csv(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if not rows:
            out.write_text("", encoding="utf-8-sig")
            return out
        columns = rows[0].keys()
        with out.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return out

    def export_json(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [dict(row) for row in self.rows()]
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def city_summary(self) -> list[dict]:
        """按城市汇总。经验/学历口径取自 `profile.py`（阈值单一真源）。

        原先这里写死两份名单，两份都不对——因为混淆了「BOSS 搜索**参数**」和
        「列表接口**返回值**」：
          · `'应届生'` 是搜索参数码 102 的标签，接口返回的是 `'在校/应届'`
            → 35 条应届岗从未被 junior_jobs 数进去
          · `'不限'` 同理，接口返回 `'学历不限'`，且缺 中专/高中/初中
            → bachelor_accessible 少算 24 条
        `profile.ALLOW_EXP` / `ALLOW_DEG` 存的就是接口返回值（那边有注释专门
        标注了这个区分），直接用它们，顺带以后调画像不用改两处。
        """
        exp_ph = ", ".join("?" * len(P.ALLOW_EXP))
        deg_ph = ", ".join("?" * len(P.ALLOW_DEG))
        query = f"""
        SELECT city,
               COUNT(*) AS jobs,
               COUNT(DISTINCT company_name) AS companies,
               ROUND(AVG((salary_min_k + salary_max_k) / 2.0), 2) AS avg_mid_salary_k,
               SUM(CASE WHEN experience IN ({exp_ph}) THEN 1 ELSE 0 END) AS junior_jobs,
               SUM(CASE WHEN education IN ({deg_ph}) THEN 1 ELSE 0 END) AS bachelor_accessible
        FROM jobs
        GROUP BY city
        ORDER BY jobs DESC
        """
        params = (*sorted(P.ALLOW_EXP), *sorted(P.ALLOW_DEG))
        return [dict(row) for row in self.conn.execute(query, params)]
