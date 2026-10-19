"""字符串归一与去重。取值与解析的真源在 `boss_scrape`，这里只做转出。

## 为什么不再自己写一份

`parse_salary` / `is_daily_rate` / 活跃度文案 / 成立年份 / 关闭文案原先在这里和
`boss_scrape/models.py` 各有一份，逻辑逐字相同。两份必然漂移，而漂移的代价是
**同一个岗位在采集链路和刷新链路得出不同结论**——降级是单向的，错了回不去。
2026-10-19 收敛成一份：真源在 `boss_scrape`，这里只转出，供 `job_radar` 的旧调用点继续用。

本模块**只留 `job_radar` 自己需要的**：去重键、位置拼接、标签合并。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from boss_scrape.models import (ACTIVE_DESC_DAYS, CLOSED_MARKERS,
                                active_days, founded_year, is_daily_rate,
                                parse_salary)
from boss_scrape.models import active_days as active_days_from_desc
from boss_scrape.models import founded_year as founded_year_from_intro

__all__ = ["ACTIVE_DESC_DAYS", "CLOSED_MARKERS", "active_days_from_desc",
           "founded_year_from_intro", "is_daily_rate", "merge_unique",
           "normalized_text", "parse_salary", "source_id_from_url",
           "split_location", "stable_id"]


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
