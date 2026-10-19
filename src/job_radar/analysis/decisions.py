"""Explainable offline triage. A shortlist is a review queue, not a hiring prediction."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from ..candidate import CandidateProfile
from ..profile import staffing_signal

BUCKETS = ("优先核实", "需核实门槛", "信息不足", "暂不优先")
DEGREES = {"初中及以下": 0, "高中": 1, "中专/中技": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
SKILL_TERMS = {
    "Python": ("python",),
    "Agent / 智能体": ("agent", "agents", "aiagent", "智能体"),
    "LLM / 大模型": ("llm", "llms", "大模型", "语言模型"),
    "RAG / 检索": ("rag", "检索增强", "向量检索"),
    "LangChain / LangGraph": ("langchain", "langgraph"),
    "API / 服务开发": ("fastapi", "flask", "django", "restful", "api"),
    "SQL / 数据库": ("sql", "mysql", "postgresql", "sqlite", "数据库"),
    "工程部署": ("docker", "kubernetes", "k8s", "ci/cd"),
    "评测 / 观测": ("评测", "评估体系", "可观测", "tracing", "evaluation"),
    "工具调用 / MCP": ("function calling", "tool calling", "mcp", "工具调用"),
    "前端 / 全栈": ("react", "vue", "typescript", "全栈"),
    "模型训练 / 微调": ("pytorch", "微调", "lora", "sft", "模型训练"),
    "工作流 / 低代码": ("dify", "coze", "n8n", "工作流"),
    "记忆 / 上下文": ("长期记忆", "记忆管理", "上下文管理", "context engineering"),
}


def parse_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, type(default)) else default
    except (ValueError, TypeError):
        return default


@lru_cache(maxsize=512)
def _term_pattern(term: str):
    # Short Latin words such as RAG must not match 'storage'.
    escaped = re.escape(term.casefold())
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])") if re.fullmatch(r"[a-z0-9 /+.#-]+", term.casefold()) else re.compile(escaped)


def mentions(text: str, terms) -> bool:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return any(_term_pattern(unicodedata.normalize("NFKC", term)).search(normalized) for term in terms if term)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def snapshot_age(row: dict, now: datetime) -> float | None:
    stamp = parse_time(row.get("detail_fetched_at"))
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 86400


def salary_mid(row: dict) -> float | None:
    if re.search(r"[/／]\s*[天日时]|[元块]\s*/?\s*[天日时]|日薪|时薪", row.get("salary_text") or ""):
        return None
    lo, hi = row.get("salary_min_k"), row.get("salary_max_k")
    if lo is None or hi is None:
        return None
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    return (lo + hi) / 2 if math.isfinite(lo) and math.isfinite(hi) and 0 < lo <= hi <= 300 else None


def closed(row: dict) -> bool:
    return bool(row.get("job_invalid")) or any(x in (row.get("job_status_desc") or "") for x in ("已关闭", "已下架", "停止招聘", "已结束"))


def safe_job_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.netloc == "www.zhipin.com" and re.fullmatch(r"/job_detail/[A-Za-z0-9_~-]+\.html", parts.path):
            return f"https://www.zhipin.com{parts.path}"
    except ValueError:
        pass
    return ""


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[\n。；;]+", text) if s.strip()]


# ============================================================================
# 判定策略：LLM 标签 → 画像
# ============================================================================
#
# 这里只做**策略映射**（哪个岗位族算对口、哪档资历算够得着），分类本身由
# 决策模型完成 —— `job_analysis.relabel` 把结果写进 role_family /
# seniority_score / fresh_grad / grad_year / employment_type / is_proxy /
# tech_stack_json。用正则解析 JD 才是要避免的事：那只能覆盖想得到的情况。

#: `role_family` → (契合度 0-3, 中文名)
_ROLE_FIT: dict[str, tuple[int, str]] = {
    "ai_app": (3, "AI 应用 / Agent 开发"),
    "algorithm": (2, "算法 / 模型训练"),
    "backend": (2, "后端开发"),
    "fullstack": (2, "全栈开发"),
    "data": (1, "数据 / 数仓"),
    "test": (1, "测试 / 质量保障"),
    "frontend": (1, "前端"),
    "ops": (1, "运维 / SRE"),
    "embedded": (0, "嵌入式"),
    "product": (0, "产品经理"),
    "operation": (0, "运营"),
    "sales": (0, "销售 / 商务"),
    "design": (0, "设计"),
    "management": (0, "项目管理"),
    "content": (0, "AIGC 内容制作"),
    "teaching": (0, "讲师 / 教研"),
    "ecommerce": (0, "电商"),
    "implementation": (0, "实施 / 驻场"),
    "other": (0, "其它"),
}
#: 契合度低于这个值的岗位族整类排除。这是**方向**问题 ——
#: 不是「门槛松一点就能投」，所以不进次级可报。
_ROLE_MIN_FIT = 2

_EMPLOYMENT_LABEL = {
    "internship": "实习岗",
    "parttime": "兼职 / 非全日制",
    "campus": "校招 / 应届专项",
    "fulltime": "全职",
}

#: `seniority_score` 是 0–4 的连续值（0=实习/应届，4=专家/管理）。
_SENIORITY_BANDS = (
    (0.6, "实习 / 应届级"),
    (1.6, "初级（约 1-3 年）"),
    (2.6, "中级（约 3-5 年）"),
    (3.6, "高级（约 5-8 年）"),
    (9.9, "专家 / 管理"),
)


def _seniority_label(value: float) -> str:
    for cap, label in _SENIORITY_BANDS:
        if value < cap:
            return label
    return _SENIORITY_BANDS[-1][1]


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tech_flat(row: dict) -> list[str]:
    """读 LLM 抽出的技术栈扁平列表。读不到返回空 —— **不退回扫正文**。"""
    data = parse_json(row.get("tech_stack_json"), {})
    flat = data.get("flat") if isinstance(data, dict) else None
    if not isinstance(flat, list):
        return []
    return [str(x) for x in flat][:40]


def evaluate(row: dict, profile: CandidateProfile, now: datetime) -> dict:
    """按画像判定一条岗位。**判据以 LLM 标签为主，不再用正则解析 JD。**

    为什么改（2026-10-19）：正则每加一条都是给上一个漏判打补丁 —— 先加
    「正文要硕博」，再加「标注岗要区分技术活和苦力」，再加「年限是期望值不是
    门槛」…… 这些判断决策模型一次就能给出，而且有固定选项集、可复核、有置信度。
    正则版本的根本问题是：**它只能覆盖你想得到的情况**，想不到的全漏，
    而且每补一条都可能像 `OD` 吃掉 `Coding` 那样误伤。

    主判据来自 `job_analysis.relabel`（`relabeled_at` 非空即有标签）：
      `role_family` / `fresh_grad` / `grad_year` / `employment_type` /
      `seniority_score` / `is_proxy` / `tech_stack_json`

    仍然保留的判据都是**结构化数据或主体身份**，不是文本解析：
      城市、薪资区间、平台在招状态、公司是不是人力服务商、雇主是否匿名。
    """
    title = row.get("title") or ""
    jd = row.get("job_description") or ""
    company = row.get("company_name") or ""
    reasons: list[str] = []
    concerns: list[str] = []
    evidence: list[str] = []
    exclusions: list[str] = []
    unresolved: list[str] = []
    score = 40.0

    # ---------------- 结构化数据 ----------------
    if closed(row):
        exclusions.append("本地快照已标记关闭")
    if profile.cities and row.get("city") not in profile.cities:
        exclusions.append("不在当前目标城市范围")
    if profile.preferred_cities and row.get("city") in profile.preferred_cities:
        score += 8
        reasons.append("属于同级优先城市")
    degree = row.get("education") or ""
    if degree in DEGREES and DEGREES[degree] > DEGREES.get(profile.education, 99):
        exclusions.append(f"列表学历要求为{degree}")

    # ---------------- LLM 标签（主判据）----------------
    relabeled = (row.get("relabeled_at") or "").strip()
    role = (row.get("role_family") or "").strip()
    fresh = (row.get("fresh_grad") or "").strip()
    grad_year = (row.get("grad_year") or "").strip()
    employment = (row.get("employment_type") or "").strip()

    if not relabeled:
        # 没有标签就不猜。用正则兜底等于把「未知」伪装成「已判」——
        # 那正是之前 48 条说不清为什么被排除的来源。
        unresolved.append("尚未打标（缺 LLM 标签），先跑 job_analysis.relabel 再判")
    else:
        if profile.exclude_internships and employment in ("internship", "parttime"):
            exclusions.append(f"LLM 判为{_EMPLOYMENT_LABEL.get(employment, employment)}")
        # 应届表态：只有「明确拒绝」才硬排除。`unstated` 不是拒绝 ——
        # 绝大多数 JD 根本不提应届，按「没提就是不要」判会误杀一大片。
        if fresh == "reject":
            exclusions.append("LLM 判为正文明确拒绝应届/毕业生")
        elif fresh == "accept":
            reasons.append("LLM 判为正文接受应届")
            score += 12
        elif fresh == "maybe":
            reasons.append("LLM 判为应届可放宽（未明确）")
            score += 5
        # 届别：只有「明确限定更晚届别」才硬排除。
        if profile.graduation_year:
            if grad_year in ("2027", "other_year"):
                exclusions.append(f"LLM 判为仅面向更晚届别（{grad_year}）")
            elif grad_year == str(profile.graduation_year):
                reasons.append(f"LLM 判为面向 {grad_year} 届")
                score += 8
        # 岗位族：这是**方向**问题，不是门槛松一点就能投的问题，所以硬排除。
        if role:
            fit, label = _ROLE_FIT.get(role, (0, role))
            if fit >= _ROLE_MIN_FIT:
                reasons.append(f"岗位族：{label}")
                score += 4 * fit
            else:
                exclusions.append(f"LLM 判为{label}，与求职方向不符")
        # 资历：**不硬排除**。招聘是许愿式的，年限是期望值不是门槛。
        seniority = _as_float(row.get("seniority_score"))
        if seniority is not None:
            level = _seniority_label(seniority)
            if seniority < 1.6:
                reasons.append(f"LLM 判为{level}")
                score += 8
            else:
                unresolved.append(f"LLM 判为{level}，年限是期望值，可尝试投递")
        # 代招/外包：决策模型有独立的 `is_proxy` 概率，比标题正则可靠。
        proxy = _as_float(row.get("is_proxy"))
        if profile.exclude_staffing and proxy is not None and proxy >= 0.5:
            exclusions.append("LLM 判为代招/外包岗位")

    # ---------------- 公司主体身份 ----------------
    if profile.exclude_staffing:
        signal = staffing_signal(row)
        if signal:
            exclusions.append(f"外包/人力服务用工（{signal}）")
        elif re.search(r"外包|外派|驻场|(?<![A-Za-z])OD(?![A-Za-z])", title, re.I):
            exclusions.append("标题含外包或代招信号")
    if profile.excluded_companies and mentions(company, profile.excluded_companies):
        exclusions.append("命中自定义公司排除词")
    if profile.excluded_titles and mentions(title, profile.excluded_titles):
        exclusions.append("命中自定义岗位排除词")
    if not company or row.get("anonymous") or re.match(r"^(?:[\u4e00-\u9fff]{2})?某", company):
        unresolved.append("雇主主体不明，需确认实际签约公司")

    # ---------------- 技术栈（LLM 抽取，不扫正文）----------------
    stack = _tech_flat(row)
    if stack:
        hits = [x for x in profile.skills if mentions(" ".join(stack), [x])]
        if hits:
            reasons.append("技术栈命中：" + "、".join(hits[:6]))
            score += min(18, 3 * len(hits))
        reasons.append("LLM 技术栈：" + "、".join(stack[:6]))

    # ---------------- 薪资 ----------------
    mid = salary_mid(row)
    if mid is None:
        unresolved.append("缺少可比较的月薪区间")
    elif float(row["salary_max_k"]) < profile.salary_min_k:
        exclusions.append("挂牌月薪上限低于期望下限")
    elif float(row["salary_min_k"]) > profile.salary_max_k:
        unresolved.append("挂牌月薪下限高于期望范围，需核实职级")
    else:
        reasons.append("挂牌月薪与期望区间有交集")
        score += 7

    # ---------------- 时间 ----------------
    age = snapshot_age(row, now)
    if age is None:
        concerns.append("没有详情/状态采集时间")
    elif age < -0.05:
        concerns.append("采集时间在未来，时间字段待核对")
    elif age > 7:
        concerns.append(f"最近详情/状态快照距今{int(age)}天，须确认仍在招")
    else:
        concerns.append("近期快照也不等于今天仍在招")

    if not jd.strip():
        unresolved.append("缺少岗位正文")

    # ---------------- 分桶 ----------------
    if exclusions:
        bucket = "暂不优先"
    elif not jd.strip():
        bucket = "信息不足"
    elif unresolved:
        bucket = "需核实门槛"
    else:
        bucket = "优先核实"
    concerns = list(dict.fromkeys(exclusions + unresolved + concerns))

    # 证据句：从正文里挑出含技术栈词的句子，供人工复核时定位。
    keywords = profile.roles + profile.skills + stack[:12]
    for sentence in _sentences(jd):
        if len(evidence) >= 3:
            break
        if len(sentence) >= 15 and mentions(sentence, keywords) and sentence[:240] not in evidence:
            evidence.append(sentence[:240])
    return {"bucket": bucket, "score": round(max(0, min(100, score - 8 * len(unresolved) - 40 * bool(exclusions))), 1),
            "reasons": reasons, "concerns": concerns, "evidence": evidence[:4],
            # 排除原因必须**跟着结果一起返回**。只返回 bucket 的话，界面上只能看到
            # 「暂不优先」却看不到为什么 —— 2026-10-19 分析今晚抓的数据时，
            # 57 条里有 48 条落在这个桶，而外部拿不到任何解释。
            # 它也是 `-40 * bool(exclusions)` 那个扣分的唯一依据，不给出来就没法核对。
            "exclusions": list(exclusions),
            "unresolved": list(unresolved)}
