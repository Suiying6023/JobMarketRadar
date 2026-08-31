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


# 招聘者活跃度文案 -> 约合天数。**这是唯一真源**，两条链路都用它。
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
# 交给 `active_days_from_desc` 的正则兜底，不必也不该逐个列。
#
# ⚠️ **不要加"裸文案"条目**（如 `月内活跃`、`日内活跃`）。它们是其它短语的子串，
# 会先命中并把正则兜底挡掉：实测加了 `月内活跃` 之后 `6月内活跃` 返回 30 而不是 180。
# 库里 13 种实际文案（现场统计）也从未出现裸写法。
#
# ⚠️ **匹配按短语长度降序**，不按书写顺序 —— `本月活跃` 与 `2月内活跃` 这类
# 互相包含时，短的排前面会吃掉长的。降序保证先匹配更具体的那个。
_ACTIVE_DESC_DAYS: dict[str, int] = {
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
ACTIVE_DESC_DAYS: list[tuple[str, int]] = sorted(
    _ACTIVE_DESC_DAYS.items(), key=lambda kv: -len(kv[0]))

# 岗位关闭文案。与活跃度同理，**只在这里维护一份**。
CLOSED_MARKERS: tuple[str, ...] = (
    "职位已关闭", "该职位已关闭", "已下线", "职位已下线",
    "职位已结束", "暂停招聘", "招聘已结束",
)


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
