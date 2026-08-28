from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse


SALARY_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-~—至]\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<unit>[Kk万])(?:[·x×](?P<months>\d+)薪)?"
)
DAILY_RE = re.compile(r"(?P<lo>\d+)\s*[-~—至]\s*(?P<hi>\d+)\s*元/天")


def is_daily_rate(salary_text: str) -> bool:
    """按天计薪一般是实习/日结岗，不是我们要跟踪的正式岗位薪资。"""
    return bool(DAILY_RE.search((salary_text or "").replace(" ", "")))


def parse_salary(text: str) -> tuple[float | None, float | None, int | None]:
    value = (text or "").replace(" ", "")
    match = SALARY_RE.search(value)
    if match:
        lo = float(match.group("lo"))
        hi = float(match.group("hi"))
        if match.group("unit") == "万":
            lo *= 10
            hi *= 10
        months = int(match.group("months")) if match.group("months") else None
        return lo, hi, months
    daily = DAILY_RE.search(value)
    if daily:
        # 按 21.75 个工作日折算成月薪 K，仅用于横向粗略比较。
        return round(int(daily.group("lo")) * 21.75 / 1000, 2), round(
            int(daily.group("hi")) * 21.75 / 1000, 2
        ), None
    return None, None, None


def split_location(city: str, district: str, business: str) -> str:
    return "·".join(v for v in (city, district, business) if v)


def normalized_text(value: str) -> str:
    value = re.sub(r"\s+", "", (value or "").lower())
    value = re.sub(r"[（）()【】\[\]·•\-_/\\]", "", value)
    return value


def stable_id(*parts: str, length: int = 24) -> str:
    joined = "||".join(normalized_text(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def source_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    match = re.search(r"/job_detail/([^./?]+)", parsed.path)
    if match:
        return match.group(1)
    return stable_id(url)


def merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in [*left, *right]:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


# 招聘者活跃度文案 -> 天数。实测详情接口 bossInfo.activeTimeDesc 是这类中文短语，
# 直接存字符串无法比较排序，转成天数才能筛"只看 N 天内活跃的岗位"。
ACTIVE_DESC_DAYS: list[tuple[str, int]] = [
    # 「在线」是最强活跃信号（当前正在线），原先落到 None 被当成"平台未给"，
    # 反而和真正的缺失值混在一起，2026-08-10 修正为 0。
    ("在线", 0),
    ("刚刚活跃", 0),
    ("今日活跃", 0),
    ("3日内活跃", 3),
    ("本周活跃", 7),
    ("2周内活跃", 14),
    ("本月活跃", 30),
    ("2月内活跃", 60),
    ("3月内活跃", 90),
    # 「近半年活跃」原先正则也兜不住（"半年"里没有数字），补进列举。
    ("近半年活跃", 180),
    ("半年前活跃", 180),
    ("半年内活跃", 180),
]


def active_days_from_desc(text: str) -> int | None:
    value = (text or "").strip()
    if not value:
        return None
    for phrase, days in ACTIVE_DESC_DAYS:
        if phrase in value:
            return days
    # 兜底：形如 "5日内活跃" / "2月内活跃" 的未列举组合。
    match = re.search(r"(\d+)\s*(日|天|周|月|年)", value)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        factor = {"日": 1, "天": 1, "周": 7, "月": 30, "年": 365}[unit]
        return n * factor
    return None


# BOSS 不提供公司注册时间和注册资本（那属于工商信息）。但公司简介里
# 常有“成立于2015年”这类表述，实测约半数简介能提取到年份。
FOUNDED_YEAR_RE = re.compile(
    r"(?:成立于|创立于|创建于|始建于|成立时间[:：]?)\s*(\d{4})\s*年?"
    r"|(\d{4})\s*年\s*(?:成立|创立|创建)"
)


def founded_year_from_intro(intro: str) -> int | None:
    """从公司简介里提取成立年份。取不到就返回 None，不猜。"""
    match = FOUNDED_YEAR_RE.search(intro or "")
    if not match:
        return None
    value = match.group(1) or match.group(2)
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    # 1900 之前和明显未来的年份视为误匹配（可能撞上电话或金额）。
    return year if 1900 <= year <= 2100 else None
