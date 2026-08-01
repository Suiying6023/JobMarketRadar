from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Platform(StrEnum):
    BOSS = "boss"
    LIEPIN = "liepin"
    JOB51 = "job51"
    ZHILIAN = "zhilian"


class BrowserMode(StrEnum):
    CDP = "cdp"
    PERSISTENT = "persistent"


class CityConfig(BaseModel):
    name: str
    boss_code: str | None = None


class SearchFilters(BaseModel):
    salary: str | None = None
    experience: str | None = None
    degree: str | None = None
    scale: str | None = None
    stage: str | None = None
    industry: str | None = None
    # 求职类型：全职/兼职/实习（接口参数 jobType，1901=全职）
    jobType: str | None = None

    def api_params(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


class AutoChatPolicy(BaseModel):
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
    mode: BrowserMode = BrowserMode.CDP
    cdp_url: str = "http://127.0.0.1:9222"
    user_data_dir: str = ".browser-profile"
    channel: str = "chrome"
    headless: bool = False
    navigation_timeout_ms: int = 30_000
    slow_mo_ms: int = 0


class CollectionConfig(BaseModel):
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.BOSS])
    cities: list[CityConfig]
    keywords: list[str]
    pages_per_query: int = Field(default=3, ge=1, le=10)
    detail_limit_per_query: int = Field(default=30, ge=0, le=200)
    page_size: int = Field(default=30, ge=10, le=50)
    request_delay_seconds: tuple[float, float] = (6.0, 12.0)
    # 每抓一条详情后的间隔。详情接口调用量是列表的 N 倍，这里是主要的限速点。
    detail_delay_seconds: tuple[float, float] = (3.0, 7.0)
    max_requests_per_run: int = Field(default=500, ge=1, le=5000)
    filters: list[SearchFilters] = Field(default_factory=lambda: [SearchFilters()])
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_company_keywords: list[str] = Field(default_factory=lambda: ["猎头", "人力资源", "劳务派遣"])
    collect_details: bool = True
    database_path: str = "data/jobs.sqlite3"
    raw_dir: str = "data/raw"
    output_dir: str = "output"
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    auto_chat: AutoChatPolicy = Field(default_factory=AutoChatPolicy)


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
    # 从活跃度文案解析出的天数，0 表示今天活跃过。None 表示平台没给。
    recruiter_active_days: int | None = None
    recruiter_online: bool = False
    # 代招（猎头/外包/人力公司）与岗位是否已失效，来自详情接口。
    is_proxy_job: bool = False
    job_invalid: bool = False
    address: str = ""
    # 以下字段都来自详情接口，抓详情时零额外请求成本。
    recruiter_certificated: bool = False
    # 招聘者与公司的稳定 ID。公司名可能是“某大型互联网公司”这类马甲，
    # brand_id 才能可靠聚合同一家公司；recruiter_id 能识别一人挂多岗。
    recruiter_id: str = ""
    brand_id: str = ""
    company_intro: str = ""
    company_founded_year: int | None = None
    # 平台给的岗位新旧标签（“最新”/“招聘中”）。BOSS 不提供精确发布时间。
    job_status_desc: str = ""
    position_name: str = ""
    recruitment_count: str = ""
    latitude: float | None = None
    longitude: float | None = None
    job_description: str = ""
    skills: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    welfare: list[str] = Field(default_factory=list)
    matched_queries: list[str] = Field(default_factory=list)
    source_rank: int | None = None
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
    skipped: int = 0
    auto_chat_planned: int = 0
    auto_chat_executed: int = 0
    requests_used: int = 0
    stopped_reason: str = ""
