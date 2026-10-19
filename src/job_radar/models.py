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
    max_page_number: int = Field(default=10, ge=1, le=30)
    max_keywords_per_run: int = Field(default=3, ge=1, le=50)
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.BOSS])
    cities: list[CityConfig]
    keywords: list[str]
    pages_per_query: int = Field(default=3, ge=1, le=10)
    list_source: Literal["api", "native"] = "api"
    pages_per_keyword: dict[str, int] = Field(default_factory=dict)
    detail_limit_per_query: int = Field(default=30, ge=0, le=200)
    page_size: int = Field(default=30, ge=10, le=50)
    request_delay_seconds: tuple[float, float] = (6.0, 12.0)
    detail_delay_seconds: tuple[float, float] = (3.0, 7.0)
    detail_batch: int = Field(default=5, ge=1, le=50)
    detail_cooldown_seconds: tuple[float, float] = (90.0, 150.0)
    burst_threshold: int = Field(default=8, ge=1, le=100)
    burst_penalty_seconds: tuple[float, float] = (60.0, 120.0)
    code37_limit: int = Field(default=35, ge=1, le=5000)
    filters: list[SearchFilters] = Field(default_factory=lambda: [SearchFilters()])
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_company_keywords: list[str] = Field(default_factory=list)
    # 平台自己的岗位类型字段（`raw_json.jobType`）。2026-10-18 用 11329 条实测标定：
    #   0 = 全职（10995 条）、4 = 实习（96 条日薪岗全是它）、
    #   5 = 校招/应届（标题样本：「应届生」「27届校招」）、6 = 兼职（「兼职/远程」「讲师」）。
    #
    # ⚠️ **5 绝不能排除**：它正是「已毕业、走校招通道」的岗位，粗暴排除 jobType≠0
    # 会连带干掉 101 条校招岗。默认只排除 4（实习）与 6（兼职）。
    exclude_job_types: list[int] = Field(default_factory=lambda: [4, 6])
    # 每发多少个接口请求就重铸一次 `__zp_stoken__`。
    #
    # 2026-10-18 实测（已登录会话，间隔 6 秒）：
    #   · 第 5 个请求成功，第 6 个回 `code=37`「您的环境存在异常.」
    #   · 干等 30 秒 → 仍是 37；干等 90 秒 → 仍是 37
    #   · 做一次普通页面加载 → 立刻恢复，再撑 5 个
    # 所以窗口是**按请求次数**的，等待不解决，只有重铸能续。
    #
    # 默认 4（比实测的 5 留一个余量）：主动重铸，让 code=37 根本不发生；
    # 一旦发生，现有的持久熔断逻辑仍然是兜底，不在这里放松。
    #
    # ⚠️ 2026-10-18 晚些时候的实测把这条降级为**兜底**：每个响应都会下发新的
    # 种子三件套（`__zp_sseed__`/`__zp_sname__`/`__zp_sts__`），可以就地算出新
    # stoken，成本 0 个请求（见 `collectors/stoken.py`）。开着 `stoken_roll` 时
    # 这条只在滚动失败后起作用，不再驱动导航。
    remint_after_requests: int = Field(default=4, ge=1, le=50)
    # 是否按平台自己的算法滚动 `__zp_stoken__`。
    #
    # 实测（2026-10-18）：同一 token 只允许 5 个请求，第 6 个 `code=37`；
    # 每个 code=0 响应下发新种子，算出来的 token 立刻可用 —— 连续 20/20 全过，
    # 零额外请求。关掉它就退回"每 N 个请求整页导航一次"（一次 92~993 个请求）。
    stoken_roll: bool = True
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


def job_from_row(row) -> JobRecord:
    """库里的行 → `JobRecord`。**唯一实现**，别在调用处另写一份。

    两处细节都是踩过的：

    - `*_json` 列可能是 NULL，`json.loads` 会直接炸，所以缺省补 `[]` / `{}`。
    - `collected_at` 来自外部快照时可能是无效时间或未来时间。无效的直接丢掉让模型取
      默认值，而不是让 pydantic 抛——一条脏时间不该让整批导入失败。
    """
    import json

    value = dict(row)
    for key in ("skills", "labels", "welfare", "matched_queries"):
        value[key] = json.loads(value.pop(key + "_json", "[]") or "[]")
    value["raw"] = json.loads(value.pop("raw_json", "{}") or "{}")
    if value.get("collected_at") and not _valid_time(value["collected_at"]):
        value.pop("collected_at")
    return JobRecord.model_validate(
        {key: item for key, item in value.items() if key in JobRecord.model_fields})


def _valid_time(value) -> bool:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)
