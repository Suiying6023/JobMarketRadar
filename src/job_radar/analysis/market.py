"""Read-only, reproducible analysis of the complete local sampling pool."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import statistics
import unicodedata
import threading
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..candidate import CandidateProfile
from .decisions import (BUCKETS, SKILL_TERMS, closed, evaluate, mentions,
                        parse_json, parse_time, safe_job_url, salary_mid, snapshot_age)

READ_COLUMNS = (
    "canonical_id", "title", "company_name", "brand_id", "city", "district", "salary_text",
    "salary_min_k", "salary_max_k", "salary_months", "experience", "education", "company_size",
    "financing_stage", "industry", "job_url", "job_description", "skills_json", "matched_queries_json",
    "glm_tags_json", "glm_tags_updated_at", "job_invalid", "job_status_desc", "is_proxy_job",
    "retention_status", "bookmarked", "detail_fetched_at", "first_seen_at", "last_seen_at", "updated_at",
)


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"岗位数据库不存在：{target.name}；请指定已有数据库或先导入本地数据")
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _company(row: dict) -> str:
    return row.get("brand_id") or re.sub(r"\s+", "", row.get("company_name") or "") or row["canonical_id"]


def _company_name(row: dict) -> str:
    return re.sub(r"\s+", "", row.get("company_name") or "") or "未提供"


def _duplicate_key(row: dict) -> tuple:
    return (_company(row), re.sub(r"[\s·•]+", "", row.get("title") or "").casefold(), row.get("city") or "")


class MarketSnapshot:
    def __init__(self, path: str | Path, now: datetime | None = None):
        self.path = Path(path).resolve()
        self.now = now or datetime.now(timezone.utc)
        self.rows: list[dict] = []
        self.progress: list[dict] = []
        self.ledger: list[dict] = []
        self.legacy_actions: dict[str, dict] = {}
        self.orphan_groups = 0
        self.quick_check = "unknown"
        self._overview = None
        self._cache_lock = threading.RLock()
        self._ranks: dict[str, list[tuple[dict, dict]]] = {}
        with closing(connect_readonly(self.path)) as conn:
            try:
                conn.execute("BEGIN")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "jobs" not in tables:
                    raise ValueError("所选数据库缺少 jobs 表")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
                required = {"canonical_id", "title", "job_description"}
                if not required.issubset(columns):
                    raise ValueError("所选 jobs 表缺少必要字段，先用迁移工具检查数据库版本")
                selection = ",".join(f'"{c}"' if c in columns else f"NULL AS \"{c}\"" for c in READ_COLUMNS)
                if "raw_json" in columns:
                    selection += ", CASE WHEN json_valid(raw_json) THEN json_extract(raw_json,'$.anonymous') ELSE NULL END AS anonymous"
                self.rows = [dict(row) for row in conn.execute("SELECT " + selection + " FROM jobs ORDER BY canonical_id")]
                if "keyword_progress" in tables:
                    self.progress = [dict(r) for r in conn.execute("SELECT city,keyword,last_page,exhausted,last_new,updated_at FROM keyword_progress")]
                if "cross_platform_groups" in tables:
                    self.orphan_groups = conn.execute("SELECT COUNT(*) FROM cross_platform_groups g LEFT JOIN jobs j USING(canonical_id) WHERE j.canonical_id IS NULL").fetchone()[0]
                if "request_log" in tables:
                    self.ledger = [dict(r) for r in conn.execute("SELECT day,kind,outcome,code,COUNT(*) AS n FROM request_log GROUP BY day,kind,outcome,code ORDER BY day DESC")]
                if "chat_actions" in tables:
                    for action in conn.execute("SELECT canonical_id,action,status,message FROM chat_actions ORDER BY id"):
                        state = self.legacy_actions.setdefault(action["canonical_id"], {})
                        if action["action"] == "applied" and action["status"] == "done":
                            state["applied"] = True
                        elif action["action"] == "note":
                            state["note"] = action["message"]
                self.quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.rollback()
        self.by_id = {r["canonical_id"]: r for r in self.rows}
        self._duplicates = Counter(_duplicate_key(r) for r in self.rows)

    def ranked(self, profile: CandidateProfile) -> list[tuple[dict, dict]]:
        with self._cache_lock:
            return self._ranked_unlocked(profile)

    def _ranked_unlocked(self, profile: CandidateProfile) -> list[tuple[dict, dict]]:
        key = profile.model_dump_json()
        if key not in self._ranks:
            rows = [(r, evaluate(r, profile, self.now)) for r in self.rows]
            rows.sort(key=lambda pair: (BUCKETS.index(pair[1]["bucket"]), -pair[1]["score"], pair[0]["canonical_id"]))
            # A small bounded cache keeps profile editing cheap without retaining every revision.
            if len(self._ranks) >= 3:
                self._ranks.clear()
            self._ranks[key] = rows
        return self._ranks[key]

    def item(self, row: dict, decision: dict, details: bool = False) -> dict:
        age = snapshot_age(row, self.now)
        tags = parse_json(row.get("glm_tags_json"), {})
        result = {
            "id": row["canonical_id"], "title": row.get("title") or "", "company": row.get("company_name") or "",
            "city": row.get("city") or "", "salary": row.get("salary_text") or "未提供",
            "salary_min_k": row.get("salary_min_k"), "salary_max_k": row.get("salary_max_k"),
            "experience": row.get("experience") or "未提供", "education": row.get("education") or "未提供",
            "company_size": row.get("company_size") or "", "industry": row.get("industry") or "未提供",
            "url": safe_job_url(row.get("job_url") or ""), "detail_fetched_at": row.get("detail_fetched_at") or "",
            "stale": age is None or age > 7 or age < -0.05,
            "snapshot_age_days": round(age, 1) if age is not None else None,
            "snapshot_status": "已标关闭" if closed(row) else "快照未标关闭，当前待核实",
            "decision": decision, "skills": parse_json(row.get("skills_json"), []),
            "bookmarked": bool(row.get("bookmarked")), "applied": False, "note": "",
            "duplicate_count": self._duplicates[_duplicate_key(row)],
        }
        result.update(self.legacy_actions.get(row["canonical_id"], {}))
        if details:
            result.update(job_description=row.get("job_description") or "", matched_queries=parse_json(row.get("matched_queries_json"), []),
                          legacy_match=tags.get("match") or "未标注", legacy_match_reason=tags.get("match_reason") or "",
                          legacy_retention=row.get("retention_status") or "", first_seen_at=row.get("first_seen_at") or "")
        return result

    def select(self, profile: CandidateProfile, q: str = "", city: str = "", tier: str = "") -> list[tuple[dict, dict]]:
        q = q.strip().casefold()
        selected = []
        for row, decision in self.ranked(profile):
            if city and row.get("city") != city:
                continue
            if tier and decision["bucket"] != tier:
                continue
            if q and q not in " ".join(str(row.get(k) or "") for k in ("title", "company_name", "city", "job_description")).casefold():
                continue
            selected.append((row, decision))
        return selected

    def _groups(self, column: str) -> list[dict]:
        groups: dict[str, list] = defaultdict(list)
        for row in self.rows:
            groups[row.get(column) or "未提供"].append(row)
        result = []
        for name, rows in groups.items():
            salaries = [v for r in rows if (v := salary_mid(r)) is not None]
            result.append({"name": name, "jobs": len(rows), "jd": sum(bool((r.get("job_description") or "").strip()) for r in rows),
                           "salary_n": len(salaries), "median_salary_k": round(statistics.median(salaries), 2) if salaries else None,
                           "companies": len({_company_name(r) for r in rows})})
        return sorted(result, key=lambda r: (-r["jobs"], r["name"]))

    def overview(self, profile: CandidateProfile | None = None) -> dict:
        with self._cache_lock:
            return self._overview_unlocked(profile)

    def _overview_unlocked(self, profile: CandidateProfile | None = None) -> dict:
        if self._overview is None:
            rows = self.rows
            details = [r for r in rows if (r.get("job_description") or "").strip()]
            salary_rows = [v for r in rows if (v := salary_mid(r)) is not None]
            invalid_json = {column: sum(bool(r.get(column)) and not self._valid_json(r[column]) for r in rows)
                            for column in ("glm_tags_json", "skills_json", "matched_queries_json")}
            tagged = sum(bool(parse_json(r.get("glm_tags_json"), {})) for r in rows)
            oldest = min((r.get("first_seen_at") for r in rows if r.get("first_seen_at")), default="")
            newest = max((r.get("last_seen_at") for r in rows if r.get("last_seen_at")), default="")
            latest_detail = max((r.get("detail_fetched_at") for r in rows if r.get("detail_fetched_at")), default="")
            ages = [snapshot_age(r, self.now) for r in details]
            old = sum(age is not None and age > 7 for age in ages)
            missing_age = sum(age is None for age in ages)
            future = sum(age is not None and age < -0.05 for age in ages)
            skill_counts = []
            for name, terms in SKILL_TERMS.items():
                count = sum(mentions(r["job_description"], terms) for r in details)
                skill_counts.append({"name": name, "count": count, "denominator": len(details),
                                     "percent": round(100 * count / len(details), 1) if details else 0})
            skill_counts.sort(key=lambda r: -r["count"])
            duplicate_groups = sum(n > 1 for n in self._duplicates.values())
            duplicate_excess = sum(n - 1 for n in self._duplicates.values())
            same_text: dict[str, list[dict]] = defaultdict(list)
            for row in details:
                text = re.sub(r"\s+", "", row["job_description"])
                if len(text) >= 120:
                    same_text[text].append(row)
            cross_name_groups = [group for group in same_text.values() if len({_company_name(r) for r in group}) > 1]
            salary_bands = []
            for lo, hi, label in [(0, 10, "<10K"), (10, 15, "10–<15K"), (15, 20, "15–<20K"), (20, 30, "20–<30K"), (30, 50, "30–<50K"), (50, 301, "≥50K")]:
                salary_bands.append({"label": label, "count": sum(lo <= v < hi for v in salary_rows), "denominator": len(salary_rows)})
            families = defaultdict(list)
            for row in rows:
                title = row.get("title") or ""
                if mentions(title, ("agent", "agents", "aiagent", "智能体")):
                    family = "标题含 Agent / 智能体"
                elif mentions(title, ("大模型", "llm")):
                    family = "标题含大模型 / LLM（不含上项）"
                elif re.search(r"算法|深度学习|机器学习|NLP", title, re.I):
                    family = "其他算法相关标题"
                elif re.search(r"全栈|前端", title):
                    family = "其他全栈 / 前端标题"
                elif re.search(r"python|后端", title, re.I):
                    family = "其他 Python / 后端标题"
                else:
                    family = "其他标题"
                families[family].append(row)
            title_families = []
            for family, members in families.items():
                mids = [v for r in members if (v := salary_mid(r)) is not None]
                title_families.append({"name": family, "jobs": len(members), "jd": sum(bool((r.get("job_description") or "").strip()) for r in members),
                                       "salary_n": len(mids), "median_salary_k": statistics.median(mids) if mids else None})
            self._overview = {
                "scope": "全国整库的本地采样池；未按个人画像过滤，不代表全国招聘总体",
                "as_of": self.now.isoformat(),
                "observation_window": {"first_seen": oldest, "last_seen": newest, "latest_detail_or_status": latest_detail},
                "totals": {"jobs": len(rows), "with_jd": len(details), "closed": sum(closed(r) for r in rows),
                           "open_snapshot": sum(not closed(r) for r in rows), "tagged": tagged, "stale": old,
                           "unknown_status": len(rows) - len(details) + missing_age,
                           "duplicate_groups": duplicate_groups, "duplicate_excess": duplicate_excess,
                           "active_legacy": sum(r.get("retention_status") == "active" for r in rows),
                           "companies": len({_company_name(r) for r in rows}), "company_identity_groups": len({_company(r) for r in rows}), "salary_n": len(salary_rows),
                           "median_salary_k": round(statistics.median(salary_rows), 2) if salary_rows else None},
                "cities": self._groups("city"), "industries": self._groups("industry"),
                "experience": self._groups("experience"), "education": self._groups("education"),
                "company_sizes": self._groups("company_size"), "financing": self._groups("financing_stage"),
                "salary_bands": salary_bands, "title_families": sorted(title_families, key=lambda r: -r["jobs"]),
                "cross_name_same_jd": {"groups": len(cross_name_groups), "rows": sum(len(g) for g in cross_name_groups)},
                "skills": skill_counts,
                "quality": [
                    {"label": "数据库快速检查", "value": self.quick_check, "detail": "只读 PRAGMA quick_check；不会修改原表"},
                    {"label": "没有 JD 正文", "value": len(rows) - len(details), "detail": "不能纳入正文技能比例的分母"},
                    {"label": "超过 7 天的详情/状态快照", "value": old, "detail": "分母为有 JD 的岗位；不是今天的招聘状态"},
                    {"label": "有正文但缺时间", "value": missing_age, "detail": "时效未知；不按新鲜岗位推荐"},
                    {"label": "未来时间戳", "value": future, "detail": "可能存在时区或机器时钟问题"},
                    {"label": "疑似重复的额外记录", "value": duplicate_excess, "detail": "公司标识+规范化标题+城市相同，只提示合并候选，不删原记录"},
                    {"label": "失去岗位关联的分组行", "value": self.orphan_groups, "detail": "历史 cross_platform_groups 悬空项；不影响此报告分母"},
                    {"label": "同文但公司名称不同的分组", "value": len(cross_name_groups), "detail": "规范化正文至少 120 字；可能是同集团、代招或模板，不足以判断企业真实性"},
                    {"label": "损坏的 JSON 字段", "value": sum(invalid_json.values()), "detail": "技能、关键词或旧模型标签无法解码的字段数"},
                ],
                "methodology": [
                    "按 canonical_id 计数；疑似同公司同标题重复另行提示，跨平台分组不当成已验证去重。",
                    "公司数按去除空白后的公司名称计数，不等于企业法人数量；同名、别名、匿名雇主会影响该数。",
                    "城市、行业、经验分布分母是全库；技能为正文词语出现率，分母只取有 JD 的岗位，可多选，不能相加。",
                    "采集历史偏向 AI/Python、低经验与本科及以下，且详情补抓经过个人画像筛选；未观测到的岗位无法反推。",
                    "薪资是有效挂牌月薪区间中点的中位数（K/月），不是录用薪资；日薪、时薪、未知或异常值被排除，未按薪资月数折算年收入，也未折算奖金。",
                    "非空 JD 均纳入技能词频；同文检测只看去空白后至少 120 字的正文，避免短模板误配，两者分母不同。",
                    "疑似岗位重复按品牌 ID（缺失时退回公司名）+标题+城市检测；同文异名按公司名称检测，两项可能重叠，不能相减或相互推算。",
                    "行业字段来自平台公司行业标签，不是严格的岗位业务场景；词语出现不证明岗位实际在建设 Agent。",
                    "旧 glm 标签和 active 分层只展示历史记录，不作为新的求职可达性结论。",
                    "detail_fetched_at 被旧详情/状态刷新链路共用，不能据此证明岗位正文也在该时间更新。",
                ],
                "request_ledger": self.ledger,
            }
        result = dict(self._overview)
        if profile is not None:
            buckets = Counter(d["bucket"] for _, d in self.ranked(profile))
            result["candidate_funnel"] = [{"label": bucket, "count": buckets[bucket]} for bucket in BUCKETS]
            result["candidate_profile"] = profile.model_dump(mode="json")
        return result

    @staticmethod
    def _valid_json(value) -> bool:
        try:
            json.loads(value)
            return True
        except (TypeError, ValueError):
            return False

    def keywords(self, config) -> dict:
        groups: dict[str, list] = defaultdict(list)
        norm = lambda term: unicodedata.normalize("NFKC", term).strip().casefold()
        configured = {norm(term): term for term in config.keywords}
        labels = dict(configured)
        for row in self.rows:
            queries = parse_json(row.get("matched_queries_json"), [])
            canonical_queries = set()
            for query in queries:
                if isinstance(query, str) and query.strip():
                    key = norm(query)
                    canonical_queries.add(key)
                    labels.setdefault(key, query)
            for key in canonical_queries:
                groups[key].append(row)
        for term in config.keywords:
            groups.setdefault(norm(term), [])
        result = []
        for key, rows in groups.items():
            keyword = labels[key]
            progress = [p for p in self.progress if norm(p["keyword"]) == key]
            result.append({"keyword": keyword, "jobs": len(rows),
                           "with_jd": sum(bool((r.get("job_description") or "").strip()) for r in rows),
                           "open_snapshot": sum(not closed(r) for r in rows),
                           "unique_jobs": sum(len({norm(q) for q in parse_json(r.get("matched_queries_json"), []) if isinstance(q, str) and q.strip()}) == 1 for r in rows),
                           "current_pages": config.pages_for(keyword),
                           "last_page": max((p["last_page"] for p in progress), default=0),
                           "enabled": key in configured, "progress": progress})
        return {"items": sorted(result, key=lambda x: (not x["enabled"], -x["jobs"], x["keyword"])),
                "caveat": "关键词按 Unicode 规范化并合并英文大小写，可重叠，不可把覆盖数相加；独有覆盖只在已观测关键词范围内成立，不能据此推断真实市场规模或最优翻页深度。"}


def csv_export(items: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["岗位ID", "公司", "岗位", "城市", "挂牌薪资", "经验", "学历", "复核优先级", "排序分", "依据", "待核实", "详情或状态快照时间", "原始链接"])
    for item in items:
        d = item["decision"]
        values = [item["id"], item["company"], item["title"], item["city"], item["salary"], item["experience"], item["education"],
                  d["bucket"], d["score"], "；".join(d["reasons"]), "；".join(d["concerns"]), item["detail_fetched_at"], item["url"]]
        # Prevent text from being interpreted as spreadsheet formulas when exported.
        writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def write_analysis(snapshot: MarketSnapshot, profile: CandidateProfile, output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    report = snapshot.overview(profile)
    report["review_queue"] = [snapshot.item(row, decision, details=True) for row, decision in snapshot.ranked(profile) if decision["bucket"] in BUCKETS[:2]][:100]
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
