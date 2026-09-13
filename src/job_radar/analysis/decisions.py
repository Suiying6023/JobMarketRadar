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


def fresh_graduate_evidence(text: str) -> str:
    # A bare '应届' is not enough: '不接受应届生' says the opposite.
    for sentence in _sentences(text):
        if re.search(r"不(?:考虑|招|接受)|非应届|在校|实习生|实习岗位|每周", sentence):
            continue
        if re.search(r"(?:欢迎|接受|优秀|面向|可接收|应往届).{0,12}应届|应届.{0,12}(?:可投|亦可|也可|均可|优先|不限|可接受)|不限.{0,8}应届|应届(?:毕业)?生[，,、\s]*(?:或|优先)", sentence):
            return sentence[:240]
    return ""


def experience_requirements(text: str) -> list[tuple[int, str, bool]]:
    results = []
    chars = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    pattern = re.compile(r"(?P<n>\d{1,2}|[一二两三四五六七八九十])\s*(?:[-~～至到]\s*(?:\d{1,2}|[一二三四五六七八九十]))?\s*年(?:及以上|以上|以?上|\+)?[^\n。；;]{0,24}经验")
    for sentence in _sentences(text):
        # Company history and phrases such as '团队平均5年经验' are not applicant requirements.
        if re.search(r"团队平均|创始人|创始团队|公司成立|公司拥有|导师拥有", sentence):
            continue
        for match in pattern.finditer(sentence):
            if re.search(r"年\s*(?:及)?以[内下]", match.group(0)):
                continue
            n = chars.get(match.group("n"), 0) or int(match.group("n"))
            # A later comma-separated bonus must not waive a preceding mandatory year count.
            clause = re.split(r"[，,]", sentence[match.start():], maxsplit=1)[0]
            prefix = sentence[max(0, match.start() - 12):match.start()]
            optional = bool(re.search(r"优先|加分|更佳|最好|可放宽|非必须|非必需|不强制", clause)
                            or re.search(r"(?:加分项|优先条件|非必须|非必需|最好)[：:\s]*$", prefix))
            results.append((n, sentence[:260], optional))
    return results


def evaluate(row: dict, profile: CandidateProfile, now: datetime) -> dict:
    title = row.get("title") or ""
    jd = row.get("job_description") or ""
    text = title + "\n" + jd
    company = row.get("company_name") or ""
    reasons: list[str] = []
    concerns: list[str] = []
    evidence: list[str] = []
    exclusions: list[str] = []
    unresolved: list[str] = []
    score = 40.0
    if closed(row):
        exclusions.append("本地快照已标记关闭")
    if profile.cities and row.get("city") not in profile.cities:
        exclusions.append("不在当前目标城市范围")
    if profile.exclude_internships and re.search(r"实习|兼职|日结", title):
        exclusions.append("标题为实习、兼职或日结")
    if profile.exclude_internships and re.search(r"必须.{0,8}在校|仅限.{0,8}在校|在校生|每周.{0,8}(?:实习|到岗).{0,5}天|实习.{0,6}[3-6三四五六]天", jd):
        unresolved.append("正文含在校或实习到岗要求，需核实是否有独立全职名额")
    cohort = [int(x) if len(x) == 4 else 2000 + int(x) for x in re.findall(r"(?<!\d)(20\d{2}|\d{2})\s*届", text)]
    cohort.extend(int(x) for x in re.findall(r"(?<!\d)(20\d{2})\s*年\s*(?:应届|毕业)", text))
    for first, last in re.findall(r"(?<!\d)(20\d{2}|\d{2})\s*[/、或与至~～-]\s*(20\d{2}|\d{2})\s*届", text):
        start, end = (int(x) if len(x) == 4 else 2000 + int(x) for x in (first, last))
        cohort.extend(range(start, end + 1) if 0 <= end - start <= 5 else (start, end))
    if profile.graduation_year and cohort and profile.graduation_year not in cohort and min(cohort) > profile.graduation_year:
        exclusions.append(f"正文或标题仅提及更晚届别（{min(cohort)} 届及以后）")
    # Education and explicit entry requirements are stronger than historical LLM labels.
    degree = row.get("education") or ""
    if degree in DEGREES and DEGREES[degree] > DEGREES.get(profile.education, 99):
        exclusions.append(f"列表学历要求为{degree}")
    if re.search(r"仅限.{0,8}(?:985|211)|(?:985|211).{0,8}(?:必须|统招院校)", jd):
        unresolved.append("正文有院校限制，需逐句确认")
    elif re.search(r"(?:知名|重点|一流).{0,3}(?:院校|高校|大学)", jd):
        unresolved.append("正文提及知名或重点院校，需确认是否是硬性门槛")
    if profile.exclude_staffing and (row.get("is_proxy_job") or re.search(r"外包|外派|驻场|(?<![A-Za-z])OD(?![A-Za-z])", title, re.I)):
        exclusions.append("代招或外包用工信号")
    if profile.excluded_companies and mentions(company, profile.excluded_companies):
        exclusions.append("命中自定义公司排除词")
    if profile.excluded_titles and mentions(title, profile.excluded_titles):
        exclusions.append("命中自定义岗位排除词")
    if profile.exclude_staffing and re.search(r"猎头|劳务派遣|人力资源", company):
        exclusions.append("公司名称带中介用工信号")
    if not company or row.get("anonymous") or re.match(r"^(?:[\u4e00-\u9fff]{2})?某", company):
        unresolved.append("雇主主体不明，需确认实际签约公司")
    fresh = fresh_graduate_evidence(text)
    experience = row.get("experience") or ""
    nmatch = re.search(r"(\d+)\s*(?:[-~～]|年)", experience)
    listed_min = int(nmatch.group(1)) if nmatch and "以内" not in experience else 0
    if listed_min > profile.max_experience_years:
        if fresh:
            concerns.append(f"列表标{experience}，正文另有应届入口")
        elif listed_min >= max(3, profile.max_experience_years + 2):
            exclusions.append(f"列表要求{experience}，超出现有工作年限")
        else:
            unresolved.append(f"列表要求{experience}，需确认能否接受项目经验替代")
    mandatory = [(years, sentence) for years, sentence, optional in experience_requirements(jd) if not optional and years > profile.max_experience_years]
    for years, sentence in mandatory[:2]:
        evidence.append(sentence)
        if fresh:
            unresolved.append(f"正文同时有{years}年经验与应届入口，需确认适用条件")
        elif years >= max(3, profile.max_experience_years + 2):
            exclusions.append(f"正文出现{years}年经验要求")
        else:
            unresolved.append(f"正文出现{years}年经验要求")
    if fresh:
        reasons.append("正文明确提供应届入口")
        evidence.append(fresh)
        score += 12
    elif experience in ("经验不限", "在校/应届", "应届生", "1年以内"):
        reasons.append(f"列表经验标为{experience}")
        score += 5
    elif not experience:
        unresolved.append("列表未给经验要求")
    if profile.roles:
        hits = [x for x in profile.roles if mentions(text, [x])]
        if not hits:
            exclusions.append("未检出当前目标方向信号")
        else:
            reasons.append("方向信号：" + "、".join(hits[:4]))
            score += min(15, len(hits) * 4)
        # Mentioning an agent tool does not turn a sales/design job into an engineering role.
        if re.search(r"运营|销售|商务|美工|设计师|剪辑|主播|客服|标注员|产品经理|培训讲师", title) and not re.search(r"开发|研发|工程师", title):
            exclusions.append("标题主要职责与开发岗位不符")
    skill_hits = [x for x in profile.skills if mentions(text, [x])]
    if skill_hits:
        reasons.append("技能信号：" + "、".join(skill_hits[:6]))
        score += min(18, 3 * len(skill_hits))
    if profile.preferred_cities and row.get("city") in profile.preferred_cities:
        score += 8
        reasons.append("属于同级优先城市")
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
    if exclusions:
        bucket = "暂不优先"
    elif not jd.strip():
        bucket = "信息不足"
    elif unresolved:
        bucket = "需核实门槛"
    else:
        bucket = "优先核实"
    concerns = list(dict.fromkeys(exclusions + unresolved + concerns))
    for sentence in _sentences(jd):
        if len(evidence) >= 3:
            break
        if len(sentence) >= 15 and mentions(sentence, profile.roles + profile.skills) and sentence[:240] not in evidence:
            evidence.append(sentence[:240])
    return {"bucket": bucket, "score": round(max(0, min(100, score - 8 * len(unresolved) - 40 * bool(exclusions))), 1),
            "reasons": reasons, "concerns": concerns, "evidence": evidence[:4]}
