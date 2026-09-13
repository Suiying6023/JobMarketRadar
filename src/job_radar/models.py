from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .candidate import CandidateProfile, clean_terms
from .service_config import LLMConfig, RefreshConfig, ServiceConfig, validate_endpoint


class Platform(StrEnum):
    BOSS = "boss"
    LIEPIN = "liepin"
    JOB51 = "job51"
    ZHILIAN = "zhilian"


class BrowserMode(StrEnum):
    CDP = "cdp"
    PERSISTENT = "persistent"


BOSS_CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "全国": "100010000",   # 不限城市（BOSS 搜索页的"全国"选项）
}


class CityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    boss_code: str | None = None

    def model_post_init(self, _ctx) -> None:
        if not self.boss_code and self.name in BOSS_CITY_CODES:
            object.__setattr__(self, "boss_code", BOSS_CITY_CODES[self.name])


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    salary: str | None = None
    experience: str | None = None
    degree: str | None = None
    scale: str | None = None
    stage: str | None = None
    industry: str | None = None
    jobType: str | None = None


class AutoChatPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    dry_run: bool = True
    require_manual_confirm: bool = True
    max_actions_per_run: int = Field(default=5, ge=0, le=50)
    greeting: str = "您好，我对这个岗位感兴趣，方便进一步沟通吗？"
    min_delay_seconds: float = Field(default=8.0, ge=3.0)
    max_delay_seconds: float = Field(default=15.0, ge=3.0)

    @field_validator("max_delay_seconds")
    @classmethod
    def validate_delay(cls, value: float, info):
        min_value = info.data.get("min_delay_seconds", 3.0)
        if value < min_value:
            raise ValueError("max_delay_seconds 必须不小于 min_delay_seconds")
        return value


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: BrowserMode = BrowserMode.PERSISTENT
    cdp_url: str = "http://127.0.0.1:9222"
    user_data_dir: str = ".browser-profile"
    channel: str = "chrome"
    headless: bool = False
    navigation_timeout_ms: int = 30_000
    slow_mo_ms: int = 0
    proxy_server: str = ""
    proxy_username: str = Field(default="", max_length=200)
    proxy_password: str = Field(default="", repr=False, max_length=4096)
    block_heavy_resources: bool = False

    @field_validator("proxy_server")
    @classmethod
    def validate_proxy(cls, value):
        return validate_endpoint(value, proxy=True)
    window_position: str = Field(default="",
                                 pattern=r"^$|^\d+,\d+$")


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    network_enabled: bool = False
    candidate: CandidateProfile | None = None
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    refresh: RefreshConfig = Field(default_factory=RefreshConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget_mode: Literal["reference", "enforce"] = "reference"
    max_page_number: int = Field(default=10, ge=1, le=30)
    max_keywords_per_run: int = Field(default=3, ge=1, le=50)
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.BOSS])
    cities: list[CityConfig]
    keywords: list[str]
    pages_per_query: int = Field(default=3, ge=1, le=10)
    pages_per_keyword: dict[str, int] = Field(default_factory=dict)
    detail_limit_per_query: int = Field(default=30, ge=0, le=200)
    page_size: int = Field(default=30, ge=10, le=50)
    request_delay_seconds: tuple[float, float] = (6.0, 12.0)
    detail_delay_seconds: tuple[float, float] = (3.0, 7.0)
    max_requests_per_run: int = Field(default=500, ge=1, le=5000)
    detail_batch: int = Field(default=5, ge=1, le=50)
    detail_cooldown_seconds: tuple[float, float] = (90.0, 150.0)
    burst_threshold: int = Field(default=8, ge=1, le=100)
    burst_penalty_seconds: tuple[float, float] = (60.0, 120.0)
    max_requests_per_day: int = Field(default=600, ge=1, le=5000)
    over_quota_action: str = Field(default="stop", pattern="^(stop|slow)$")
    over_quota_ladder: list[tuple[float, float]] = Field(
        default_factory=lambda: [(1.0, 2.0), (1.25, 3.0), (1.5, 4.0), (2.0, 6.0)]
    )
    code37_limit: int = Field(default=35, ge=1, le=5000)
    filters: list[SearchFilters] = Field(default_factory=lambda: [SearchFilters()])
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_company_keywords: list[str] = Field(default_factory=list)
    collect_details: bool = True
    database_path: str = "data/jobs.sqlite3"
    raw_dir: str = "data/raw"
    output_dir: str = "output"
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    auto_chat: AutoChatPolicy = Field(default_factory=AutoChatPolicy)

    @field_validator("pages_per_keyword")
    @classmethod
    def validate_pages_per_keyword(cls, value: dict[str, int], info):
        """页数必须在 1-30；关键词必须真的在 keywords 里（拼错会静默失效）。"""
        known = set(info.data.get("keywords") or [])
        for kw, pages in value.items():
            if not 1 <= pages <= 30:
                raise ValueError(f"pages_per_keyword[{kw}]={pages} 超出 1-30")
            if known and kw not in known:
                raise ValueError(
                    f"pages_per_keyword 里的 {kw!r} 不在 keywords 列表中"
                    "（拼写不一致会让这条覆盖静默失效）"
                )
        return value

    def pages_for(self, keyword: str) -> int:
        """该关键词本轮翻多少页。未单独配置的用 pages_per_query。"""
        return self.pages_per_keyword.get(keyword, self.pages_per_query)

    @field_validator("keywords", "include_title_keywords", "exclude_title_keywords", "exclude_company_keywords")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        return clean_terms(values)

    @field_validator("request_delay_seconds", "detail_delay_seconds", "detail_cooldown_seconds", "burst_penalty_seconds")
    @classmethod
    def validate_range(cls, value: tuple[float, float]) -> tuple[float, float]:
        import math
        if not all(math.isfinite(v) and 0 <= v <= 86400 for v in value) or value[0] > value[1]:
            raise ValueError("等待区间必须为 0–86400 秒，且下限不大于上限")
        return value

    @model_validator(mode="after")
    def validate_plan(self):
        if not self.cities or not self.keywords or not self.platforms or not self.filters:
            raise ValueError("城市、关键词、平台和筛选组合不能为空")
        if len(self.keywords) > 5000 or len(self.cities) > 500:
            raise ValueError("城市最多 500 个，关键词最多 5000 个")
        if any(not c.name.strip() for c in self.cities):
            raise ValueError("城市名称不能为空")
        if len({c.name for c in self.cities}) != len(self.cities):
            raise ValueError("城市不能重复")
        import math
        last = 0.0
        for threshold, factor in self.over_quota_ladder:
            if not math.isfinite(threshold) or not math.isfinite(factor) or threshold <= last or factor < 1:
                raise ValueError("降速档位必须按阈值递增，倍数必须为大于等于 1 的有限数")
            last = threshold
        return self


class JobRecord(BaseModel):
    platform: Platform
    source_job_id: str
    job_url: str
    company_url: str = ""
    title: str
    company_name: str = ""
    city: str = ""
    district: str = ""
    business_district: str = ""
    location_text: str = ""
    salary_text: str = ""
    salary_min_k: float | None = None
    salary_max_k: float | None = None
    salary_months: int | None = None
    experience: str = ""
    education: str = ""
    company_size: str = ""
    financing_stage: str = ""
    industry: str = ""
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_active_status: str = ""
    recruiter_active_days: int | None = None
    recruiter_online: bool = False
    is_proxy_job: bool = False
    job_invalid: bool = False
    address: str = ""
    recruiter_certificated: bool = False
    recruiter_id: str = ""
    brand_id: str = ""
    company_intro: str = ""
    company_founded_year: int | None = None
    job_status_desc: str = ""
    position_name: str = ""
    recruitment_count: str = ""
    latitude: float | None = None
    longitude: float | None = None
    job_description: str = ""
    detail_fetched_at: str = ""
    skills: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    welfare: list[str] = Field(default_factory=list)
    matched_queries: list[str] = Field(default_factory=list)
    source_rank: int | None = None
    retention_status: str = "active"
    bookmarked: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def canonical_key(self) -> str:
        return f"{self.platform.value}:{self.source_job_id}"


class CollectionStats(BaseModel):
    queries: int = 0
    list_records: int = 0
    detail_success: int = 0
    detail_failed: int = 0
    inserted: int = 0
    updated: int = 0
    auto_chat_planned: int = 0
    auto_chat_executed: int = 0
    requests_used: int = 0
    stopped_reason: str = ""
