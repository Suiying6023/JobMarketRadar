from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import BrowserContext, Page
from patchright.async_api import Error as PlaywrightError

from ..models import AutoChatPolicy, CityConfig, CollectionConfig, JobRecord, Platform, SearchFilters
from ..normalization import (
    active_days_from_desc,
    founded_year_from_intro,
    is_daily_rate,
    parse_salary,
    source_id_from_url,
    split_location,
)
from .base import Collector
from .browser import BrowserSession


ORIGIN = "https://www.zhipin.com"
API_PATH = "/wapi/zpgeek/search/joblist.json"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
SEARCH_BASE = f"{ORIGIN}/web/geek/job"

# BOSS 的搜索页在加载过程中会短暂跳到 about:blank 再跳回，此时相对路径无法解析，
# 因此同源请求一律使用绝对地址，并在请求前等待页面回到 zhipin 域。
# 31/37 来自公开项目实测；36「您的账户存在异常行为」是本地实测命中的账号级风控；
# 32「您的账户存在异常行为，已暂时被禁止使用」是 2026-08-01 实测命中的临时封禁，
# 比 36 更严重，出现后应停止自动访问并让账号静置较长时间。
RISK_CODES = {31, 32, 36, 37}
# 关键字兜底：防止平台新增的风控码被误判成普通业务错误。
# 「操作太频繁」「滑块」来自参考项目，实测在 82 条正常详情响应里 0 命中，可安全加入。
# 参考项目还用了「验证」，但实测它在 82 条正常响应里命中 26 次——全是 JD 正文的
# 「测试验证」「效果验证」和营业执照说明「经由平台审核验证通过」，会把好数据判成风控，
# 因此不采用。
RISK_TEXTS = (
    "访问频繁",
    "安全校验",
    "环境存在异常",
    "账户存在异常",
    "操作过于频繁",
    "操作太频繁",
    "滑块",
)


RISK_DUMP_DIR = Path("data/risk_dumps")


class RiskControlStop(RuntimeError):
    """命中平台风控。必须立刻停止整轮采集，不重试。"""

FILTER_CODES = {
    "salary": {
        "3K以下": "402", "3-5K": "403", "5-10K": "404", "10-20K": "405",
        "20-50K": "406", "50K以上": "407",
    },
    "experience": {
        "在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
        "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107",
    },
    "degree": {"初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202", "本科": "203", "硕士": "204", "博士": "205"},
    "scale": {"0-20人": "301", "20-99人": "302", "100-499人": "303", "500-999人": "304", "1000-9999人": "305", "10000人以上": "306"},
    "stage": {"未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804", "C轮": "805", "D轮及以上": "806", "已上市": "807", "不需要融资": "808"},
    # 求职类型（接口参数 jobType）。1901 是「全职」，从 URL 实测确认。
    "jobType": {"全职": "1901", "兼职": "1902", "实习": "1903"},
}


class BossCollector(Collector):
    def __init__(self, config: CollectionConfig):
        self.config = config
        self.browser_session = BrowserSession(config.browser)
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.chat_count = 0
        self.request_count = 0
        self.last_lid = ""
        self.owns_page = False

    async def start(self) -> None:
        self.context = await self.browser_session.start()
        # 优先复用浏览器里已经登录并加载好的 zhipin 标签页。
        # 实测直接打开 https://www.zhipin.com/ 会经 /{城市}/?seoRefer=index 最终跳到
        # about:blank，新开的空白页反而进不去；复用已有页签最稳。
        for existing in self.context.pages:
            if ORIGIN in (existing.url or ""):
                self.page = existing
                self.owns_page = False
                break
        if self.page is None:
            self.page = await self.context.new_page()
            self.owns_page = True
        self.page.set_default_timeout(self.config.browser.navigation_timeout_ms)

    async def close(self) -> None:
        # 只关闭自己开的页签，不动用户原有的登录页。
        if self.page and self.owns_page:
            await self.page.close()
        await self.browser_session.close()

    async def _goto_search(self, url: str, attempts: int = 3) -> None:
        """打开搜索页。页面可能瞬时停在 about:blank，重试即可恢复。"""
        assert self.page is not None
        for attempt in range(1, attempts + 1):
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(random.randint(2500, 4000))
            if ORIGIN in (self.page.url or ""):
                return
            if attempt < attempts:
                await asyncio.sleep(random.uniform(2.0, 4.0))
        raise RuntimeError(f"搜索页反复跳到 {self.page.url}，无法继续")

    @staticmethod
    def _filter_value(name: str, value: str | None) -> str | None:
        """把筛选中文标签映射成接口码；逗号分隔的多选逐个映射后拼接。

        BOSS 多选筛选在 URL 里是逗号分隔（实测 experience=101,104 生效），
        配置里写「经验不限,1-3年」即可。找不到映射的原始值原样透传，
        方便直接写码。
        """
        if not value:
            return None
        mapping = FILTER_CODES.get(name, {})
        if "," in value:
            return ",".join(mapping.get(part.strip(), part.strip()) for part in value.split(","))
        return mapping.get(value, value)

    def _params(self, city: CityConfig, keyword: str, filters: SearchFilters, page: int) -> dict[str, str | int]:
        if not city.boss_code:
            raise ValueError(f"城市 {city.name} 缺少 boss_code")
        params: dict[str, str | int] = {
            "scene": "1",
            "query": keyword,
            "city": city.boss_code,
            "page": page,
            "pageSize": self.config.page_size,
        }
        for name, value in filters.model_dump().items():
            code = self._filter_value(name, value)
            if code:
                params[name] = code
        return params

    async def _settle_on_origin(self, timeout_ms: int = 15_000) -> None:
        """等待页面停在 zhipin 域。搜索页初始化时会短暂停在 about:blank。"""
        assert self.page is not None
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if ORIGIN in (self.page.url or ""):
                return
            await self.page.wait_for_timeout(250)
        raise RuntimeError(f"页面未停留在 {ORIGIN}，当前为 {self.page.url}")

    async def _evaluate_fetch(self, url: str, attempts: int = 3) -> dict:
        """在页面里发同源请求。

        BOSS 的安全校验会在加载后触发一次跳转，正好撞上 evaluate 时 Playwright 会抛
        "Execution context was destroyed"。这不是风控，等页面稳定后重试即可。
        token 在 Python 侧持有（会话内稳定），避免页面刷新把 _PAGE.token 重置。
        """
        assert self.page is not None
        # BOSS 对不带 token 头的请求给极低配额（实测约 12-13 次/窗口），页面自己的
        # 请求带齐 token/zp_token/traceId 所以能持续跑。token 的合法来源是
        # window._PAGE.token（登录后由 __zp_stoken__ 派生）。zp_token 来自 cookie bst，
        # fetch credentials:'include' 会自动带。traceId 每次请求重新生成，这里暂不构造。
        script = """async ({url, token}) => {
            try {
                const headers = {
                    credentials: 'include',
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest',
                };
                if (token) headers['token'] = token;
                const response = await fetch(url, headers);
                const text = await response.text();
                return {status: response.status, text};
            } catch (err) {
                return {status: -1, text: '', error: String(err)};
            }
        }"""
        token = await self._read_token()
        for attempt in range(1, attempts + 1):
            if self.request_count >= self.config.max_requests_per_run:
                raise RiskControlStop(
                    f"已达单次运行请求上限 {self.config.max_requests_per_run}，主动停止。"
                )
            self.request_count += 1
            try:
                return await self.page.evaluate(script, {"url": url, "token": token})
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                    raise
                await self._settle_on_origin()
                # 页面刷新后 token 可能要重新读
                token = await self._read_token()
                await asyncio.sleep(random.uniform(1.5, 3.0))
        raise RuntimeError("同源请求重试耗尽")  # pragma: no cover

    async def _read_token(self) -> str:
        """读 window._PAGE.token（登录会话内稳定）。页面刷新会临时重置，重试几次。"""
        page = self.page
        if page is None:
            return ""
        for _ in range(3):
            try:
                return str(
                    await page.evaluate(
                        """() => (typeof window._PAGE !== 'undefined' && window._PAGE)
                                     ? (window._PAGE.token || '') : ''"""
                    )
                )
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                await asyncio.sleep(2)
        return ""

    @staticmethod
    def _dump_risk_body(path: str, status: int, raw_text: str) -> None:
        """把风控响应正文原样落盘，用于事后判断风控形态。

        风控响应同样是 HTTP 200，正文可能是验证页 HTML 而不是 JSON。只有留下
        原文，才能区分「可人工过验证」和「硬封」。落盘失败不能影响主流程——
        这里已经在抛 RiskControlStop 的路上了，不要用 IO 错误盖掉真实原因。
        """
        try:
            RISK_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9]+", "_", path)[:60].strip("_")
            target = RISK_DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{status}-{name}.txt"
            target.write_text(raw_text, encoding="utf-8")
        except OSError:
            pass

    async def _same_origin_fetch(self, path: str, recover_code37: bool = False) -> dict:
        assert self.page is not None
        await self._settle_on_origin()
        result = await self._evaluate_fetch(
            f"{ORIGIN}{path}" if path.startswith("/") else path
        )
        status = int(result.get("status", 0))
        if result.get("error"):
            raise RuntimeError(f"同源请求失败: {result['error']}")
        raw_text = result.get("text") or ""
        if "环境存在异常" in raw_text and recover_code37:
            # 2026-08-01 突破：code=37（stoken 过期）可通过让真实浏览器访问
            # security-check.html 让页面 JS 重算 __zp_stoken__，随后重试即成功。
            # 不需要等 25 分钟冷却窗口。
            body = json.loads(raw_text or "{}")
            zp = body.get("zpData") or {}
            seed = str(zp.get("seed") or "")
            name = str(zp.get("name") or "")
            ts = str(zp.get("ts") or "")
            if seed and name and ts:
                self._dump_risk_body(path, status, raw_text)
                await self._recover_code37(seed, name, ts)
                result = await self._evaluate_fetch(
                    f"{ORIGIN}{path}" if path.startswith("/") else path
                )
                status = int(result.get("status", 0))
                raw_text = result.get("text") or ""
        if any(token in raw_text for token in RISK_TEXTS):
            # 2026-07-30 实测：节流响应是 HTTP 200 + {"code": 0, "message": "访问频繁，请稍后再试"}。
            # 注意 code 是 0，所以下面所有基于 code 的判断都会放过它，这段关键字检查
            # 是唯一的防线。正文不是验证页 HTML，没有滑块可过，只能等窗口恢复。
            # 仍然落盘，因为平台改文案时这里是唯一能看到原始正文的地方。
            self._dump_risk_body(path, status, raw_text)
            raise RiskControlStop(f"响应命中风控关键字，HTTP {status}，正文已存 {RISK_DUMP_DIR}")
        try:
            body = json.loads(raw_text or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"BOSS 接口返回非 JSON，HTTP {status}") from exc
        code = body.get("code")
        message = str(body.get("message") or "")
        if status in (401, 403, 429) or code in RISK_CODES:
            raise RiskControlStop(f"命中平台风控 HTTP {status} code={code}: {message}")
        if code not in (None, 0):
            raise RuntimeError(f"BOSS 接口错误 code={code}: {message}")
        return body

    async def _recover_code37(self, seed: str, name: str, ts: str) -> None:
        """让真实浏览器访问 security-check.html 重算 __zp_stoken__。

        实测（2026-08-01）：导航到 security-check 页后页面 JS 约 3-6 秒自动生成
        新 stoken，回搜索页后重试原请求即成功。参考 BossAutomation 项目验证。
        """
        assert self.page is not None
        sec_url = (
            f"{ORIGIN}/web/common/security-check.html?"
            f"{urlencode({'seed': seed, 'name': name, 'ts': ts, 'callbackUrl': '/web/geek/jobs'})}"
        )
        await self.page.goto(sec_url, wait_until="domcontentloaded",
                             timeout=self.config.browser.navigation_timeout_ms)
        await self.page.wait_for_timeout(6000)  # 等页面 JS 生成新 stoken
        # 回搜索页，恢复 fetch 所需的 zhipin 域环境
        await self.page.goto(
            f"{SEARCH_BASE}?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=self.config.browser.navigation_timeout_ms,
        )
        await self.page.wait_for_timeout(4000)

    def _parse_api_job(self, raw: dict, keyword: str, rank: int) -> JobRecord:
        encrypt_job_id = str(raw.get("encryptJobId") or "")
        job_url = f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html" if encrypt_job_id else ""
        city = str(raw.get("cityName") or "")
        district = str(raw.get("areaDistrict") or "")
        business = str(raw.get("businessDistrict") or "")
        salary = str(raw.get("salaryDesc") or "")
        salary_min, salary_max, salary_months = parse_salary(salary)
        return JobRecord(
            platform=Platform.BOSS,
            source_job_id=encrypt_job_id or source_id_from_url(job_url),
            job_url=job_url,
            company_url=(
                f"https://www.zhipin.com/gongsi/{raw.get('encryptBrandId')}.html"
                if raw.get("encryptBrandId") else ""
            ),
            title=str(raw.get("jobName") or ""),
            company_name=str(raw.get("brandName") or ""),
            city=city,
            district=district,
            business_district=business,
            location_text=split_location(city, district, business),
            salary_text=salary,
            salary_min_k=salary_min,
            salary_max_k=salary_max,
            salary_months=salary_months,
            experience=str(raw.get("jobExperience") or ""),
            education=str(raw.get("jobDegree") or ""),
            company_size=str(raw.get("brandScaleName") or ""),
            financing_stage=str(raw.get("brandStageName") or ""),
            industry=str(raw.get("brandIndustry") or ""),
            recruiter_name=str(raw.get("bossName") or ""),
            recruiter_title=str(raw.get("bossTitle") or ""),
            recruiter_active_status=str(raw.get("activeTimeDesc") or ("在线" if raw.get("bossOnline") else "")),
            skills=[str(v) for v in (raw.get("skills") or []) if v],
            labels=[str(v) for v in (raw.get("jobLabels") or []) if v],
            welfare=[str(v) for v in (raw.get("welfareList") or []) if v],
            matched_queries=[keyword],
            source_rank=rank,
            raw=raw,
        )

    def _should_keep(self, job: JobRecord) -> bool:
        title = job.title.lower()
        company = job.company_name.lower()
        if self.config.include_title_keywords and not any(
            word.lower() in title for word in self.config.include_title_keywords
        ):
            return False
        if any(word.lower() in title for word in self.config.exclude_title_keywords):
            return False
        if any(word.lower() in company for word in self.config.exclude_company_keywords):
            return False
        # 日薪岗基本都是实习/日结，即使标题没写“实习”也按薪资口径排除。
        if is_daily_rate(job.salary_text):
            return False
        return bool(job.title and job.job_url)

    async def _collect_detail(self, job: JobRecord) -> JobRecord:
        """从详情接口补全 JD 与招聘者活跃度。

        实测（2026-07-29）搜索列表接口不返回 activeTimeDesc，只有详情接口的
        bossInfo 里有；JD 正文在 jobInfo.postDescription，是干净的纯文本，
        不需要解析 DOM，也就不受前端 class 名改版影响。
        详情接口必须带列表响应里的 securityId 和 lid，否则会被打回空白页。
        """
        raw = job.raw
        security_id = str(raw.get("securityId") or "")
        if not security_id:
            raise RuntimeError("列表记录缺少 securityId，无法请求详情接口")
        params = {"securityId": security_id, "lid": str(raw.get("lid") or self.last_lid or "")}
        # recover_code37=True：触发 code=37（stoken 过期）时自动走 security-check 重算重试
        body = await self._same_origin_fetch(
            f"{DETAIL_PATH}?{urlencode(params)}", recover_code37=True
        )
        zp = body.get("zpData") or {}
        # 平台节流时返回 code=0 但没有 zpData.jobInfo（实测 82 条正常响应全部有 jobInfo）。
        # 不加这道判断的话，被节流的记录会被当成"成功但 JD 为空"静默写库，
        # 之后再也不会被 backfill 选中（它只挑 job_description = ''）。
        if not zp.get("jobInfo"):
            raise RuntimeError(
                f"详情响应缺少 zpData.jobInfo（code={body.get('code')}），疑似被节流，不写库"
            )
        job_info = zp.get("jobInfo") or {}
        boss_info = zp.get("bossInfo") or {}
        brand_info = zp.get("brandComInfo") or {}

        description = str(job_info.get("postDescription") or "").strip()
        if description:
            job.job_description = re.sub(r"\n{3,}", "\n\n", description)

        # activeTimeDesc 只有详情接口给：2026-07-30 核对 897 条纯列表记录，
        # 列表响应连这个键都没有（bossOnline 有，但 535/1001 为 true，明显是
        # 「近期在线」的宽松口径，不能当活跃度用）。

        active_desc = str(boss_info.get("activeTimeDesc") or "")
        if active_desc:
            job.recruiter_active_status = active_desc
            job.recruiter_active_days = active_days_from_desc(active_desc)
        job.recruiter_online = bool(boss_info.get("bossOnline"))
        job.recruiter_certificated = bool(boss_info.get("certificated"))
        if boss_info.get("name"):
            job.recruiter_name = str(boss_info["name"])
        if boss_info.get("title"):
            job.recruiter_title = str(boss_info["title"])
        # 招聘者唯一 ID：同一个 ID 挂大量岗位是僵尸/中介信号，公司名不可靠时靠它聚合。
        if job_info.get("encryptUserId"):
            job.recruiter_id = str(job_info["encryptUserId"])
        job.job_status_desc = str(job_info.get("jobStatusDesc") or "")
        job.position_name = str(job_info.get("positionName") or "")
        job.recruitment_count = str(job_info.get("recruitmentCountDesc") or "")
        if job_info.get("latitude") is not None:
            job.latitude = float(job_info["latitude"])
        if job_info.get("longitude") is not None:
            job.longitude = float(job_info["longitude"])

        # proxyJob/proxyType 非 0 表示代招（猎头、外包、人力公司）。
        job.is_proxy_job = bool(job_info.get("proxyJob")) or bool(job_info.get("proxyType"))
        job.job_invalid = bool(job_info.get("invalidStatus"))
        if job_info.get("address"):
            job.address = str(job_info["address"])
        for skill in job_info.get("showSkills") or []:
            text = str(skill or "").strip()
            if text and text not in job.skills:
                job.skills.append(text)
        if brand_info.get("scaleName"):
            job.company_size = str(brand_info["scaleName"])
        if brand_info.get("stageName"):
            job.financing_stage = str(brand_info["stageName"])
        if brand_info.get("industryName"):
            job.industry = str(brand_info["industryName"])
        # brandId 比公司名可靠：BOSS 上有大量「某大型互联网公司」这类匿名马甲，
        # 只有 ID 能把它们聚合成同一家。
        if brand_info.get("encryptBrandId"):
            job.brand_id = str(brand_info["encryptBrandId"])
        intro = str(brand_info.get("introduce") or "").strip()
        if intro:
            job.company_intro = intro
            # 注册资本 BOSS 完全不给；成立年份只能从简介文本里碰运气提取。
            job.company_founded_year = founded_year_from_intro(intro)
        for label in brand_info.get("labels") or []:
            text = str(label or "").strip()
            if text and text not in job.welfare:
                job.welfare.append(text)
        job.raw["detail"] = zp
        # 详情响应会回一个轮换后的新 securityId。存回去，让下次重试用新鲜 token，
        # 免得存量记录里的旧 token 越放越久。lid 同理。
        if zp.get("securityId"):
            job.raw["securityId"] = str(zp["securityId"])
        if zp.get("lid"):
            job.raw["lid"] = str(zp["lid"])
        await asyncio.sleep(random.uniform(*self.config.detail_delay_seconds))
        return job

    async def _maybe_chat(self, job: JobRecord) -> str:
        policy: AutoChatPolicy = self.config.auto_chat
        if not policy.enabled:
            return "disabled"
        if self.chat_count >= policy.max_actions_per_run:
            return "limit_reached"
        if policy.dry_run:
            self.chat_count += 1
            return "dry_run"
        if policy.require_manual_confirm:
            answer = input(f"\n是否与 {job.company_name} / {job.title} 开聊？[y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                return "declined"
        assert self.context is not None
        page = await self.context.new_page()
        try:
            await page.goto(job.job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)
            button = page.get_by_text(re.compile(r"^(立即沟通|继续沟通)$")).first
            if not await button.count():
                return "button_not_found"
            await button.click()
            await page.wait_for_timeout(1200)
            # 默认依赖 BOSS 已设置的招呼语。若页面出现可编辑输入框，才填入配置内容。
            editor = page.locator("textarea, [contenteditable='true']").last
            if await editor.count() and policy.greeting:
                try:
                    await editor.fill(policy.greeting)
                    send = page.get_by_text(re.compile(r"^(发送|送出)$")).last
                    if await send.count():
                        await send.click()
                except Exception:
                    pass
            self.chat_count += 1
            await asyncio.sleep(random.uniform(policy.min_delay_seconds, policy.max_delay_seconds))
            return "executed"
        finally:
            await page.close()

    async def collect(
        self,
        city: CityConfig,
        keyword: str,
        filters: SearchFilters,
        pages: int,
        detail_limit: int,
    ) -> AsyncIterator[JobRecord]:
        assert self.page is not None
        params = self._params(city, keyword, filters, 1)
        visible = {k: v for k, v in params.items() if k not in ("pageSize", "scene")}
        # 搜索页初始化期间会短暂跳到 about:blank，_goto_search 内部会重试到停在 zhipin 域。
        await self._goto_search(f"{SEARCH_BASE}?{urlencode(visible)}")

        detailed = 0
        for page_index in range(1, pages + 1):
            api_params = self._params(city, keyword, filters, page_index)
            # 平台用上一页响应里的 lid 串联同一次搜索会话，带上它更接近真实翻页。
            if self.last_lid:
                api_params["lid"] = self.last_lid
            body = await self._same_origin_fetch(f"{API_PATH}?{urlencode(api_params)}")
            zp_data = body.get("zpData") or {}
            if zp_data.get("lid"):
                self.last_lid = str(zp_data["lid"])
            raw_jobs = zp_data.get("jobList") or []
            if not raw_jobs:
                break
            for rank, raw in enumerate(raw_jobs, start=1):
                job = self._parse_api_job(raw, keyword, rank)
                if not self._should_keep(job):
                    continue
                if self.config.collect_details and detailed < detail_limit:
                    try:
                        job = await self._collect_detail(job)
                        detailed += 1
                    except Exception as exc:
                        job.raw["detail_error"] = str(exc)
                chat_status = await self._maybe_chat(job)
                if chat_status != "disabled":
                    job.raw["auto_chat_status"] = chat_status
                yield job
            if page_index < pages:
                low, high = self.config.request_delay_seconds
                await asyncio.sleep(random.uniform(low, high))
