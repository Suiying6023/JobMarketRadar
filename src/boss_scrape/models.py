"""数据模型与字段解析。纯函数，不碰网络。

字段名照抄平台原始 JSON 的语义（`jobName` → `title`），但只保留有分析价值的。
`raw` 留一份原文，避免以后想加字段还得重抓。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

ORIGIN = "https://www.zhipin.com"

# 薪资：`20-40K·14薪` / `1.5-2.5万` / `200-300元/天`
_SALARY_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-~—至]\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>[Kk万])"
    r"(?:[·x×](?P<months>\d+)薪)?")
_DAILY_RE = re.compile(r"(?P<lo>\d+)\s*[-~—至]\s*(?P<hi>\d+)\s*元/天")

# 招聘者活跃度文案 → 约合天数。**这是唯一真源**，采集、刷新、标注三条链路都用它。
#
# 详情接口的 `bossInfo.activeTimeDesc` 与岗位页上渲染的都是这类中文短语，
# 直接存字符串无法比较排序，转成天数才能筛「只看 N 天内活跃的岗位」。
#
# ⚠️ 曾在三处各写一份，数值已经漂开过：`今日活跃` 一处算 1 天一处算 0 天、
# `半年前活跃` 一处 999 一处 180。**后果是同一个岗位在采集链路和刷新链路
# 得出不同的降级结论**，而降级是单向不可逆的。所以别再复制这张表。
#
# 这里**只列举正则兜不住的**：不含数字（`在线`/`本周活跃`/`近半年活跃`），
# 或含数字但字面意思与算术不符。带数字的常规组合（`7日内活跃`、`6月内活跃`）
# 交给 `active_days` 的正则兜底，不必也不该逐个列。
#
# ⚠️ **不要加"裸文案"条目**（如 `月内活跃`、`日内活跃`）。它们是其它短语的子串，
# 会先命中并把正则兜底挡掉：实测加了 `月内活跃` 之后 `6月内活跃` 返回 30 而不是 180。
# 库里 13 种实际文案（现场统计）也从未出现裸写法。
_ACTIVE_DAYS: dict[str, int] = {
    "在线": 0,          # 最强活跃信号：当前正在线
    "刚刚活跃": 0,
    "今日活跃": 0,
    "3日内活跃": 3,
    "本周活跃": 7,
    "2周内活跃": 14,
    "本月活跃": 30,
    "2月内活跃": 60,
    "3月内活跃": 90,
    "4月内活跃": 120,
    "5月内活跃": 150,
    "近半年活跃": 180,  # "半年"里没有数字，正则兜不住
    "半年前活跃": 180,  # 字面是"半年以前"，按 180 天处理（最不活跃的一档）
}
# ⚠️ **匹配按短语长度降序**，不按书写顺序 —— `本月活跃` 与 `2月内活跃` 这类
# 互相包含时，短的排前面会吃掉长的。降序保证先匹配更具体的那个。
ACTIVE_DESC_DAYS: list[tuple[str, int]] = sorted(
    _ACTIVE_DAYS.items(), key=lambda kv: -len(kv[0]))

# 岗位关闭文案。**只把明确的关闭文案当关闭**，其余按未判定处理：
# 误判关闭会单向降级一个在招岗位（不可逆），误判未判定只是下一轮再刷（可逆且便宜）。
CLOSED_MARKERS: tuple[str, ...] = (
    "职位已关闭", "该职位已关闭", "已下线", "职位已下线",
    "职位已结束", "暂停招聘", "招聘已结束",
)

_FOUNDED_RE = re.compile(
    r"(?:成立于|创立于|创建于|始建于|成立时间[:：]?)\s*(\d{4})\s*年?"
    r"|(\d{4})\s*年\s*(?:成立|创立|创建)")


def parse_salary(text: str) -> tuple[float | None, float | None, int | None]:
    """返回 (下限K, 上限K, 薪月数)。日薪按 21.75 天折算成月薪，仅用于横向比较。"""
    value = (text or "").replace(" ", "")
    match = _SALARY_RE.search(value)
    if match:
        lo, hi = float(match.group("lo")), float(match.group("hi"))
        if match.group("unit") == "万":
            lo, hi = lo * 10, hi * 10
        months = int(match.group("months")) if match.group("months") else None
        return lo, hi, months
    daily = _DAILY_RE.search(value)
    if daily:
        return (round(int(daily.group("lo")) * 21.75 / 1000, 2),
                round(int(daily.group("hi")) * 21.75 / 1000, 2), None)
    return None, None, None


def is_daily_rate(text: str) -> bool:
    return bool(_DAILY_RE.search((text or "").replace(" ", "")))


def active_days(desc: str) -> int | None:
    value = (desc or "").strip()
    if not value:
        return None
    for phrase, days in ACTIVE_DESC_DAYS:
        if phrase in value:
            return days
    # 兜底：形如 "5日内活跃" / "2月内活跃" 的未列举组合。
    match = re.search(r"(\d+)\s*(日|天|周|月|年)", value)
    if not match:
        return None
    factor = {"日": 1, "天": 1, "周": 7, "月": 30, "年": 365}[match.group(2)]
    return int(match.group(1)) * factor


def founded_year(intro: str) -> int | None:
    match = _FOUNDED_RE.search(intro or "")
    if not match:
        return None
    try:
        year = int(match.group(1) or match.group(2))
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _strings(value) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


@dataclass(slots=True)
class SearchSpec:
    """一次搜索请求。`filters` 直接拼进 query，用平台的筛选码。"""

    query: str
    city: str = "101210100"
    pages: int = 1
    page_size: int = 30
    filters: dict[str, str] = field(default_factory=dict)

    def params(self, page: int, lid: str = "") -> dict[str, str]:
        params = {"scene": "1", "query": self.query, "city": self.city,
                  "page": str(page), "pageSize": str(self.page_size)}
        params.update({k: str(v) for k, v in self.filters.items() if v})
        if lid:
            params["lid"] = lid
        return params


@dataclass(slots=True)
class JobCard:
    """列表响应里的一条岗位。"""

    job_id: str
    title: str
    company: str
    url: str
    company_url: str = ""
    brand_id: str = ""
    city: str = ""
    district: str = ""
    business_district: str = ""
    location_text: str = ""
    salary_text: str = ""
    salary_min_k: float | None = None
    salary_max_k: float | None = None
    salary_months: int | None = None
    daily_rate: bool = False
    experience: str = ""
    education: str = ""
    company_size: str = ""
    financing_stage: str = ""
    industry: str = ""
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_active: str = ""
    recruiter_online: bool = False
    skills: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    welfare: list[str] = field(default_factory=list)
    # 平台自己的岗位类型：0=全职、4=实习、5=校招/应届、6=兼职。
    # ⚠️ 与筛选接口的 jobTypeList（0/1901/1902/1903）是**两套枚举**，别混。
    job_type: int | None = None
    security_id: str = ""
    lid: str = ""
    keyword: str = ""
    rank: int = 0
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class JobDetail:
    """详情响应里比列表多出来的部分。"""

    job_id: str
    description: str = ""
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    job_status: str = ""
    position_name: str = ""
    recruitment_count: str = ""
    recruiter_id: str = ""
    recruiter_certificated: bool = False
    recruiter_active: str = ""
    recruiter_active_days: int | None = None
    is_proxy: bool = False
    invalid: bool = False
    company_intro: str = ""
    company_founded_year: int | None = None
    company_size: str = ""
    financing_stage: str = ""
    industry: str = ""
    brand_id: str = ""
    welfare: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # 详情响应会回一个轮换后的 securityId / lid，留着给下次重试用。
    next_security_id: str = ""
    next_lid: str = ""
    fetched_at: str = ""
    raw: dict = field(default_factory=dict)


def parse_card(raw: dict, *, keyword: str = "", rank: int = 0) -> JobCard:
    """把列表响应里的一条记录转成 JobCard。缺 job_id 直接报错，不造无主键数据。"""
    if not isinstance(raw, dict):
        raise ValueError("列表记录需要 JSON 对象")
    job_id = _text(raw.get("encryptJobId"))
    if not job_id:
        raise ValueError("列表记录缺少 encryptJobId，无法形成稳定主键")
    brand_id = _text(raw.get("encryptBrandId"))
    city, district, business = _text(raw.get("cityName")), _text(raw.get("areaDistrict")), \
        _text(raw.get("businessDistrict"))
    salary = _text(raw.get("salaryDesc"))
    lo, hi, months = parse_salary(salary)
    return JobCard(
        job_id=job_id,
        title=_text(raw.get("jobName")),
        company=_text(raw.get("brandName")),
        url=f"{ORIGIN}/job_detail/{job_id}.html",
        company_url=f"{ORIGIN}/gongsi/{brand_id}.html" if brand_id else "",
        brand_id=brand_id,
        city=city, district=district, business_district=business,
        location_text="·".join(v for v in (city, district, business) if v),
        salary_text=salary, salary_min_k=lo, salary_max_k=hi, salary_months=months,
        daily_rate=is_daily_rate(salary),
        experience=_text(raw.get("jobExperience")),
        education=_text(raw.get("jobDegree")),
        company_size=_text(raw.get("brandScaleName")),
        financing_stage=_text(raw.get("brandStageName")),
        industry=_text(raw.get("brandIndustry")),
        recruiter_name=_text(raw.get("bossName")),
        recruiter_title=_text(raw.get("bossTitle")),
        recruiter_active=_text(raw.get("activeTimeDesc")) or ("在线" if raw.get("bossOnline") else ""),
        recruiter_online=bool(raw.get("bossOnline")),
        skills=_strings(raw.get("skills")),
        labels=_strings(raw.get("jobLabels")),
        welfare=_strings(raw.get("welfareList")),
        job_type=raw.get("jobType") if isinstance(raw.get("jobType"), int) else None,
        security_id=_text(raw.get("securityId")),
        lid=_text(raw.get("lid")),
        keyword=keyword, rank=rank, raw=raw,
    )


def parse_detail(zp: dict, *, job_id: str) -> JobDetail:
    """把详情响应的 `zpData` 转成 JobDetail。

    ⚠️ 正文在 `zpData.jobInfo.postDescription`，**不在** `zpData.postDescription`。
    这一层很容易读错（我第一版探针就读错了，误以为详情不返回 JD）。
    """
    if not isinstance(zp, dict):
        raise ValueError("详情数据需要 JSON 对象")
    info = zp.get("jobInfo")
    if not isinstance(info, dict) or not info:
        raise ValueError("详情响应缺少 jobInfo")
    actual = _text(info.get("encryptId"))
    if actual and actual != job_id:
        raise RuntimeError(f"详情岗位 ID 不匹配：请求 {job_id}，返回 {actual}")
    boss = zp.get("bossInfo") or {}
    brand = zp.get("brandComInfo") or {}
    description = info.get("postDescription")
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError("详情正文为空，不能把旧正文当成新正文")

    intro = _text(brand.get("introduce"))
    welfare = _strings(brand.get("labels"))
    skills = _strings(info.get("showSkills"))
    active = _text(boss.get("activeTimeDesc"))
    return JobDetail(
        job_id=job_id,
        description=re.sub(r"\n{3,}", "\n\n", description.strip()),
        address=_text(info.get("address")),
        latitude=float(info["latitude"]) if info.get("latitude") is not None else None,
        longitude=float(info["longitude"]) if info.get("longitude") is not None else None,
        job_status=_text(info.get("jobStatusDesc")),
        position_name=_text(info.get("positionName")),
        recruitment_count=_text(info.get("recruitmentCountDesc")),
        recruiter_id=_text(info.get("encryptUserId")),
        recruiter_certificated=bool(boss.get("certificated")),
        recruiter_active=active,
        recruiter_active_days=active_days(active),
        is_proxy=bool(info.get("proxyJob")) or bool(info.get("proxyType")),
        invalid=bool(info.get("invalidStatus")),
        company_intro=intro,
        company_founded_year=founded_year(intro),
        company_size=_text(brand.get("scaleName")),
        financing_stage=_text(brand.get("stageName")),
        industry=_text(brand.get("industryName")),
        brand_id=_text(brand.get("encryptBrandId")),
        welfare=welfare, skills=skills,
        next_security_id=_text(zp.get("securityId")),
        next_lid=_text(zp.get("lid")),
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        raw=zp,
    )
