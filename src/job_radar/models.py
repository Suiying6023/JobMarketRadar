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


# BOSS 城市码。来源：`GET /wapi/zpCommon/data/cityGroup.json`（免登录）。
# 2026-08-11 核对，其中杭州与项目里长期在用的 101210100 一致，可信。
# 要补别的城市直接查那个接口，别猜——码不是简单的行政区划号。
BOSS_CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "全国": "100010000",   # 不限城市（BOSS 搜索页的"全国"选项）
}


class CityConfig(BaseModel):
    name: str
    boss_code: str | None = None

    def model_post_init(self, _ctx) -> None:
        # 没显式给码时按城市名自动补，省得每份配置都手抄一遍容易抄错
        if not self.boss_code and self.name in BOSS_CITY_CODES:
            object.__setattr__(self, "boss_code", BOSS_CITY_CODES[self.name])


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
    # 节流参数（原先写死在 HumanThrottle.__init__ 默认值里，改配置才能按账号
    # 信任度调档，不用改代码）。详情接口是 cookie 配额制，批大小和批间冷却是
    # 唯一有效防线；burst 是"连续 N 条后额外冷却"，打破稳定高速的机器特征。
    detail_batch: int = Field(default=5, ge=1, le=50)
    detail_cooldown_seconds: tuple[float, float] = (90.0, 150.0)
    burst_threshold: int = Field(default=8, ge=1, le=100)
    burst_penalty_seconds: tuple[float, float] = (60.0, 120.0)
    # 单日累计请求上限（跨进程，靠 request_log 账本执行）。
    # 2026-08-10 标定确认：封禁是**累计量制**（同 IP 累计请求量 + 账号信任度），
    # code=37 只是可恢复的配额窗口信号，不是封禁前兆。所以真正的安全线是这条，
    # 而 max_requests_per_run 只管单个进程——三个采集脚本各跑一轮就能悄悄
    # 累到 750 次而每一轮都"没超限"。
    max_requests_per_day: int = Field(default=600, ge=1, le=5000)
    # code=37 重算次数熔断阈值。不是封禁预警（见 boss.py 里的说明），
    # 只是"一轮跑了多少量"的代理指标。激进探测时放开。
    code37_limit: int = Field(default=35, ge=1, le=5000)
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
    # 详情成功获取的时间戳（活跃度是时效字段，30 天后就无意义了）。
    # 用于区分"平台真不返回活跃度"和"没抓过详情"，以及未来做"详情过期重抓"。
    detail_fetched_at: str = ""
    skills: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    welfare: list[str] = Field(default_factory=list)
    matched_queries: list[str] = Field(default_factory=list)
    source_rank: int | None = None
    # 分层保留：active=画像内投递池；sample=画像外但保留作增量去重样本；
    # purged=待物理清理。状态只由 clean/bookmark 脚本管理，采集 upsert 不覆盖。
    retention_status: str = "active"
    # 用户浏览/投递过的岗位，画像外也保留为投递目标（投递池 = active OR bookmarked）。
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
    skipped: int = 0
    auto_chat_planned: int = 0
    auto_chat_executed: int = 0
    requests_used: int = 0
    stopped_reason: str = ""
