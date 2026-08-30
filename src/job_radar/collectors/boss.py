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

from .. import profile
from ..models import AutoChatPolicy, CityConfig, CollectionConfig, JobRecord, Platform, SearchFilters
from ..normalization import (
    active_days_from_desc,
    founded_year_from_intro,
    is_daily_rate,
    parse_salary,
    source_id_from_url,
    split_location,
)
from ..refresh_fetch import quota_factor
from ..throttle import gaussian
from .base import Collector
from .browser import BrowserSession


ORIGIN = "https://www.zhipin.com"
API_PATH = "/wapi/zpgeek/search/joblist.json"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
# 必须是 jobs（带 s）。`/web/geek/job` 会 302 到 `/web/geek/jobs`，多一跳；
# 2026-08-10 标定实测：code=37 重算后回搜索页那次导航正好撞上这个 302 而超时
# （Navigation to ".../web/geek/job?..." interrupted），把重算后本该成功的详情
# 重试白白废掉一条配额。直接写终态 URL，省一跳也不再被 302 打断。
SEARCH_BASE = f"{ORIGIN}/web/geek/jobs"

# BOSS 的搜索页在加载过程中会短暂跳到 about:blank 再跳回，此时相对路径无法解析，
# 因此同源请求一律使用绝对地址，并在请求前等待页面回到 zhipin 域。
# 31/37 来自公开项目实测；36「您的账户存在异常行为」是本地实测命中的账号级风控；
# 32「您的账户存在异常行为，已暂时被禁止使用」是 2026-08-01 实测命中的临时封禁，
# 比 36 更严重，出现后应停止自动访问并让账号静置较长时间。
RISK_CODES = {31, 32, 36, 37}
# 关键字兜底：防止平台新增的风控码被误判成普通业务错误。
# 「操作太频繁」「滑块」来自参考项目，实测在 82 条正常详情响应里 0 命中，可安全加入。
# 参考项目还用了「验证」「安全校验」，但实测它们在正常 JD 里命中多次——全是正文里的
# 「测试验证」「效果验证」「数据安全校验」「安全校验通过」等，会把好数据判成风控，不采用。
# 真正的风控响应是 code=37/32/36（有独立判断）或「访问频繁」这类精确文案。
RISK_TEXTS = (
    "访问频繁",
    "环境存在异常",
    "账户存在异常",
    "操作过于频繁",
    "操作太频繁",
    "滑块",
)


RISK_DUMP_DIR = Path("data/risk_dumps")

# 一次 code=37 重算折算多少次请求。**不是 1**（2026-08-30 实测推翻）。
#
# 重算发两次导航（`security-check.html` + 回一个同域页），逐步优化的实测轨迹：
#   · 无拦截 + 回搜索页        ：发起 226 次
#   · 加资源拦截 + 回搜索页    ：真实上网 **139** 次（拦截只砍 39%）
#   · 加资源拦截 + 回 /about   ：真实上网 **37** 次  ← 现状，再省 73%
#
# 关键在**回哪个页**，不在拦什么：回搜索页要加载整个 SPA（107 次），
# 而 `_evaluate_fetch` 只要求页面停在 zhipin 域，不依赖搜索页的 DOM。
# 换成 `/about` 后那 107 次变成 1 次。
#
# 剩下的 37 次里 script 30 是 `security-check.html` 自己的挑战脚本 ——
# **这部分不可压缩**，stoken 就是靠它算出来的。所以 37 已接近这条路的下限；
# 想再低只能不走导航，而 stoken 必须由真实浏览器 JS 生成（本地算已证死路）。
#
# **改动拦截规则或回法就要重新量。**
# 探针 `scripts/probe_recover_cost.py` + `probe_recover_callback.py`，
# 数据 `data/calibration/recover-cost*.json`。
REQ_PER_RECOVER = 37


class RiskControlStop(RuntimeError):
    """命中平台风控。必须立刻停止整轮采集，不重试。"""


# 浏览器对 DNS / 连接 / TLS 失败的统一报法。**与账号状态无关**，重试即可自愈。
# 风控走的是另一条路（HTTP 200 + 验证页正文 → _dump_risk_body → RiskControlStop），
# 永远不会命中这里，所以在这里重试不会掩盖风控信号。
_TRANSIENT_FETCH_ERRORS = (
    "Failed to fetch",        # Chromium：连接失败的通用报法
    "NetworkError",           # 部分场景下的等价报法
    "Load failed",            # WebKit 同义
    "ERR_NETWORK_CHANGED",    # 切网/IP 变化
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_NAME_NOT_RESOLVED",  # DNS 抖动（TUN 环境下常见）
    "ERR_INTERNET_DISCONNECTED",
    "ERR_TIMED_OUT",
)


def _is_transient_fetch_error(result: dict) -> bool:
    """页面内 fetch 是否因**瞬时网络问题**失败（可重试），而非风控或业务错误。

    判据只看 `error` 字段：有 error 说明 fetch 抛了异常、连响应都没拿到。
    风控与业务错误都是正常响应（有 status 和 text），不会走到这里。
    """
    err = str(result.get("error") or "")
    return bool(err) and any(sig in err for sig in _TRANSIENT_FETCH_ERRORS)


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
        # 越线提示的节流游标（见 _check_daily_budget）。日线现在是降速线而非停线，
        # 提示每 50 次请求打一次就够——每次都打会把真正的风控行淹掉。
        self._last_overquota_notice = -10**9
        self.code37_count = 0  # security-check 重算次数，高频会升级到 32 封禁
        # code=37 熔断阈值。2026-08-10/11 标定：code=37 是每约 6 条一次的配额窗口
        # 信号（间距实测全程稳定 6.0），所以抓 N 条详情正常会触发约 N/6 次。
        #
        # ⚠️ 这个阈值**不是封禁预警**：code=37 密度不随累计量上升（无记忆滑动窗口），
        # 而封禁与 code=37 频率无关——08-03 实测「156 条仅 1 次 code=37 就直接
        # code=32」，另一次「108 条 21 次 code=37 零封禁」。它只是"跑了多少量"的
        # 代理指标，作用是防止一轮跑太久。
        # 从 config 读，激进探测时可放开（config.aggressive.yaml 设到 2000）。
        self.code37_limit = getattr(config, "code37_limit", 35)
        self.last_lid = ""
        self.owns_page = False
        # 已有 JD 的 canonical_id：详情接口是最贵资源（封禁红线 150-200 条），
        # 由 runner 从库里灌进来，命中即跳过详情，配额只花在真正缺 JD 的岗位上。
        self.skip_detail_ids: set[str] = set()
        self.detail_skipped = 0
        # 请求账本（可选注入）。封禁按累计量算，而三个采集脚本各跑各的进程，
        # 只有落到同一个 DB 表里才能回答"今天一共发了多少"。没注入就是纯内存计数，
        # 行为不变（不让账本成为采集的硬依赖）。
        self.ledger = None  # type: ignore[var-annotated]
        self.ledger_script = ""
        # 本轮各类请求计数（标定用：区分 list / detail / security_check 的风险差异）
        self.kind_counts: dict[str, int] = {}

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

    @staticmethod
    def _kind_of(url: str) -> str:
        """按 URL 判断请求类型。风险差异很大：详情接口几乎承担了全部风控。"""
        if DETAIL_PATH in url:
            return "detail"
        if "security-check" in url:
            return "security_check"
        if "/recommend/" in url:
            return "recommend"
        return "list"

    def over_quota_factor(self) -> float:
        """越过日线后的延迟乘数（1.0 = 未越线或配置为 stop）。

        档位表从 `config.over_quota_ladder` 读，**不写死在代码里**：
        日线是我们自设的预算（不是平台给的数字），越线后该停还是该降速、
        降多少，取决于当次任务目标 —— 那是调用方的决策，不该固化成代码。
        """
        if self.ledger is None or self.config.over_quota_action != "slow":
            return 1.0
        try:
            used = self.ledger.request_counts().get("total", 0)
        except Exception:
            return 1.0
        return quota_factor(used, self.config.max_requests_per_day,
                            self.config.over_quota_ladder)

    def _check_daily_budget(self) -> None:
        """日线执行点。`over_quota_action` 决定停还是降速。

        两种都保留而不是二选一：摸边界时要停（越线即异常最清晰），
        赶进度时要降速跑完。默认仍是 `stop`（历史行为，不改变既有调用方）。
        """
        if self.ledger is None:
            return
        try:
            used = self.ledger.request_counts().get("total", 0)
        except Exception:
            return
        cap = self.config.max_requests_per_day
        if used < cap:
            return
        if self.config.over_quota_action == "stop":
            raise RiskControlStop(
                f"今日累计请求已达 {used}/{cap}（跨进程账本），主动停止。"
                "封禁按累计量算，明日再跑。"
            )
        # slow：只观测，减速发生在 _collect_detail 的条内延迟里。
        # 每 50 次请求提示一次——每次都打会把真正的风控行淹掉。
        if self.request_count - self._last_overquota_notice >= 50:
            self._last_overquota_notice = self.request_count
            print(f"[OVER-QUOTA] 今日累计 {used}/{cap}（{used/max(cap,1):.2f}x），"
                  f"降速 {self.over_quota_factor():.0f}x 继续。"
                  f"风控信号（code=36/32、验证页）仍会立刻停止。", flush=True)

    def _count_request(self, url: str, outcome: str = "ok", code: int | None = None) -> None:
        """本轮内存计数 + 落账本。账本写失败不能打断采集——请求已经发出去了，
        记不上账是观测问题，把采集中断反而更糟（可能留下半写状态）。"""
        kind = self._kind_of(url)
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        if self.ledger is None:
            return
        try:
            self.ledger.log_request(
                kind, outcome=outcome, code=code, script=self.ledger_script
            )
        except Exception:
            pass

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
        # ⚠️ 2026-08-10 两处修复：
        #
        # ① headers 必须嵌在 init 的 `headers` 键里。原先写成 `fetch(url, headers)`
        #    （headers 当 init 顶层字段）—— fetch 只认 method/headers/body/credentials
        #    等白名单键，Accept/token/X-Requested-With 全被静默丢弃。`credentials`
        #    恰好是合法键，所以 cookie 一直带上、请求照样成功，把 bug 完全掩盖。
        #    本地 http server 实测：旧写法服务端收到 token=undefined、accept=*/*。
        #
        # ② 被动监听前端真实请求后改用正确的头集合。**前端一个 `token` 头都不发**
        #    （8 个 wapi 请求全部 token=<无>），它发的是 `zp_token`（= cookie bst 的
        #    值，前端手动复制到头里）+ `x-requested-with` + `traceid`。所以旧代码执着
        #    的 window._PAGE.token 方向本身就是错的——而且 _PAGE 在当前前端版本里
        #    根本不存在（hasPage: false）。
        #    bst 不是 HttpOnly，JS 可直接从 document.cookie 读。
        script = """async ({url}) => {
            try {
                const readCookie = (name) => {
                    const m = document.cookie.match(
                        new RegExp('(?:^|; )' + name + '=([^;]*)'));
                    return m ? decodeURIComponent(m[1]) : '';
                };
                const headers = {
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest',
                };
                const bst = readCookie('bst');
                if (bst) headers['zp_token'] = bst;
                const response = await fetch(url, {credentials: 'include', headers});
                const text = await response.text();
                return {status: response.status, text};
            } catch (err) {
                return {status: -1, text: '', error: String(err)};
            }
        }"""
        # zp_token 在页面内从 document.cookie 现读，不需要 Python 侧传 token。
        # （旧代码走 window._PAGE.token，该对象在当前前端版本已不存在，
        # 且前端真实请求从不发 token 头——见上方注释②。）
        for attempt in range(1, attempts + 1):
            # ⚠️ `max_requests_per_run` 仍是**硬停线**，但它的作用只是"别让一个进程
            # 无限跑"（进程越久，浏览器状态越可能漂）。它不是风控线，所以调用方
            # 重起一段就能继续 —— 与日线（已改为降速线）性质不同，别把两者合并。
            if self.request_count >= self.config.max_requests_per_run:
                raise RiskControlStop(
                    f"已达单次运行请求上限 {self.config.max_requests_per_run}，主动停止。"
                )
            self._check_daily_budget()
            self.request_count += 1
            self._count_request(url)
            try:
                result = await self.page.evaluate(script, {"url": url})
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                    raise
                await self._settle_on_origin()
                await asyncio.sleep(random.uniform(1.5, 3.0))
                continue
            # 页面内 fetch 抛异常时 evaluate 本身是**成功**的（回一个带 error 的对象），
            # 所以这类失败不会走上面的 except —— 必须在这里单独判，否则网络抖一下
            # 就永久放弃这条岗位。2026-08-29 实测：一次波动连废 12 条，
            # 每条都已记账、JD 没拿到、还留在待补队列里等下一轮再花一次配额。
            if _is_transient_fetch_error(result) and attempt < attempts:
                await self._settle_on_origin()
                await asyncio.sleep(random.uniform(2.0, 4.0))
                continue
            return result
        raise RuntimeError("同源请求重试耗尽")  # pragma: no cover

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
                self.code37_count += 1
                print(f"  [code=37] 第 {self.code37_count} 次重算 stoken（高频会封号）", flush=True)
                await self._recover_code37(seed, name, ts)
                result = await self._evaluate_fetch(
                    f"{ORIGIN}{path}" if path.startswith("/") else path
                )
                status = int(result.get("status", 0))
                raw_text = result.get("text") or ""
        try:
            body = json.loads(raw_text or "{}")
        except json.JSONDecodeError as exc:
            # 非 JSON 正文 = 验证页 HTML（滑块/验证码），按风控停机。只在解析失败时扫全文，
            # 避免 JSON 里的 JD 正文误报（如爬虫岗 JD 会提到「滑块」）。
            if any(token in raw_text for token in RISK_TEXTS):
                self._dump_risk_body(path, status, raw_text)
                raise RiskControlStop(f"响应命中风控关键字，HTTP {status}，正文已存 {RISK_DUMP_DIR}")
            raise RuntimeError(f"BOSS 接口返回非 JSON，HTTP {status}") from exc
        code = body.get("code")
        message = str(body.get("message") or "")
        # 2026-07-30 实测：节流响应是 HTTP 200 + {"code": 0, "message": "访问频繁，请稍后再试"}。
        # 注意 code 是 0，所以下面所有基于 code 的判断都会放过它，这段 message 关键字检查
        # 是唯一的防线。只查 message 字段不查全文——JD 正文里的「滑块」「安全校验」等是
        # 正常内容（尤其爬虫/安全岗），扫全文会误报丢弃好数据。正文不是验证页 HTML，
        # 没有滑块可过，只能等窗口恢复。仍然落盘，因为平台改文案时这里是唯一能看到原始正文的地方。
        if any(token in message for token in RISK_TEXTS):
            self._dump_risk_body(path, status, raw_text)
            self._mark_outcome("throttled", 0)
            raise RiskControlStop(f"响应命中风控关键字，HTTP {status}，正文已存 {RISK_DUMP_DIR}")
        if status in (401, 403, 429) or code in RISK_CODES:
            self._mark_outcome("risk", code if isinstance(code, int) else None)
            raise RiskControlStop(f"命中平台风控 HTTP {status} code={code}: {message}")
        if code not in (None, 0):
            raise RuntimeError(f"BOSS 接口错误 code={code}: {message}")
        return body

    def _mark_outcome(self, outcome: str, code: int | None) -> None:
        """把账本里最后一条记录改判为风控结果。

        记账发生在请求**发出前**（否则请求发了但进程崩掉就漏账），此时还不知道
        响应是什么，所以风控结论只能事后回填到刚写的那一行。
        """
        if self.ledger is None:
            return
        try:
            self.ledger.conn.execute(
                "UPDATE request_log SET outcome = ?, code = ? "
                "WHERE id = (SELECT MAX(id) FROM request_log)",
                (outcome, code),
            )
            self.ledger.conn.commit()
        except Exception:
            pass

    async def _recover_code37(self, seed: str, name: str, ts: str) -> None:
        """让真实浏览器访问 security-check.html 重算 __zp_stoken__。

        实测（2026-08-01）：导航到 security-check 页后页面 JS 约 3-6 秒自动生成
        新 stoken，回搜索页后重试原请求即成功。参考 BossAutomation 项目验证。
        2026-08-03 优化：不固定等 6s，改轮询 __zp_stoken__ cookie 直到刷新即回，
        每批省约 4s（通常是 2-5s 而非 6s）。
        """
        assert self.page is not None and self.context is not None
        sec_url = (
            f"{ORIGIN}/web/common/security-check.html?"
            f"{urlencode({'seed': seed, 'name': name, 'ts': ts, 'callbackUrl': '/web/geek/jobs'})}"
        )
        # 重算本身要发两次导航（security-check + 回搜索页），都是真实请求且都消耗
        # 信用——封禁页原文「请勿频繁提交刷新请求」印证刷新也算。旧代码只数 fetch，
        # 于是重算的开销在账上完全隐形，"今天发了多少"被系统性低估。
        #
        # ⚠️ 这里**仍然只记 1 行**，但那不代表它只值 1 次请求 ——
        # 实测一次重算真实上网 139 次（见 REQ_PER_RECOVER 的注释）。
        #
        # 为什么不改成记 139 行：`max_requests_per_day` 的现值（900）是按
        # **旧口径（重算记 1 行）** 标定的，而那条线的依据是实测的
        # 「674 条详情安全 / 1019 条触发验证挑战」—— 那是**详情条数**的边界。
        # 一旦改成记 139 行，900 这道闸会在约 34 条详情处就拦死，
        # 远低于已知安全的 674，等于用一个记账口径的变化把采集能力砍掉 20 倍。
        #
        # 正确做法是**账本口径与安全线一起换**（换成按详情条数计，或把日线
        # 按新口径重标）。那是一次独立改动，需要重新标定；在它落地之前，
        # 这里保持旧口径，而把真实成本写在 REQ_PER_RECOVER 和文档里，
        # 让「估风险」时用真实数字，「执行闸门」时用已标定的数字。
        # ⚠️ 别只改一半 —— 改记账不改闸门，闸门的含义就悄悄变了。
        self._count_request(sec_url, outcome="code37_recover", code=37)
        await self.page.goto(sec_url, wait_until="domcontentloaded",
                             timeout=self.config.browser.navigation_timeout_ms)
        # 轮询新 stoken 就绪（HttpOnly，须用 context.cookies()），比固定等待快
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await self.page.wait_for_timeout(500)
            try:
                cookies = await self.context.cookies()
                st = next((c for c in cookies if c["name"] == "__zp_stoken__"), None)
                if st and st.get("value"):
                    break
            except Exception:
                pass
        # 回一个**轻量同域页**恢复 fetch 环境，而不是完整搜索页 SPA。
        #
        # `_evaluate_fetch` 唯一的要求是「页面停在 zhipin 域」
        # （`_settle_on_origin` 的判据就是 `ORIGIN in page.url`）——
        # 它在页面里发同源 fetch，**不依赖搜索页的任何 DOM 或 JS 状态**。
        # 而回搜索页要加载整个 SPA：实测 **107 次**真实请求
        # （script 49、image 49、xhr 38…），是整次重算成本的绝大部分。
        #
        # 实测三种回法（`scripts/probe_recover_callback.py`）：
        #   搜索页 SEARCH_BASE : 107 次，fetch 可用
        #   轻量页 /about      : **1 次**，fetch 可用  ← 现用
        # 即一次重算从 139 次降到约 33 次，**省 76%**，而能力没有任何损失。
        await self.page.goto(
            f"{ORIGIN}/about",
            wait_until="domcontentloaded",
            timeout=self.config.browser.navigation_timeout_ms,
        )
        await self.page.wait_for_timeout(800)

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
        """列表阶段的准入判定 —— **这是唯一花不可退配额的一环**。

        ⚠️ 判据必须与 `profile` 同源。原先只读 config 的 YAML 名单，而
        `profile.py` 开头声称"各脚本一律 from job_radar.profile import"，
        采集侧是唯一没收敛进来的一环，于是两份名单各自漂移：
        实测 profile 有而 YAML 缺 `猎头`/`劳务派遣`（标题）、
        `人力资源`/`华钦`/`新致软件`/`汉得`（公司）。

        更贵的是**城市与薪资从不在这里判**，而它们在列表响应里就已知。
        实测代价（2026-08-30）：3207 条注定被 clean_local_db 降级的岗位里，
        **828 条已经花掉了详情配额**（超纲 596、匿名 112、公司 69、标题 30、
        非目标城市 10）—— 一条详情就是一次不可退的请求。
        超纲一项就占 596 条，而 `salary_min_k`/`salary_max_k` 全部来自列表。

        YAML 名单保留为**追加**而非替代：配置可以按轮次临时加词，
        但不能让它成为绕过 profile 基线的通道。
        """
        title = job.title.lower()
        company = job.company_name.lower()
        if self.config.include_title_keywords and not any(
            word.lower() in title for word in self.config.include_title_keywords
        ):
            return False
        # profile 基线 + config 追加，两份都要过
        if profile.match_keyword(job.title, profile.EXCLUDE_TITLE):
            return False
        if any(word.lower() in title for word in self.config.exclude_title_keywords):
            return False
        if profile.match_keyword(job.company_name, profile.EXCLUDE_COMPANY):
            return False
        if any(word.lower() in company for word in self.config.exclude_company_keywords):
            return False
        # OD 要按词边界判（裸子串会吃掉 Coding，见 profile.outsourced_od）
        if profile.outsourced_od(job.title):
            return False
        # 届别错位：只能从标题识别，JD 正文里没有
        if profile.future_cohort(job.title):
            return False
        # 匿名公司在列表阶段就能判（raw.anonymous），必须在这里拦：
        # 标成 active 就会去抓详情，而投递前无法核实公司身份的岗位最终一定被
        # clean_local_db 降级 —— 等于每条白花一次详情配额。
        if profile.is_anonymous(job.raw, job.company_name):
            return False
        # 城市与薪资：列表响应里就已知，**前移到这里能省下最大一笔配额**
        if not profile.city_allowed(job.city or ""):
            return False
        if profile.salary_out_of_range(job.salary_min_k, job.salary_max_k,
                                      job.salary_months):
            return False
        # 日薪岗基本都是实习/日结，即使标题没写“实习”也按薪资口径排除。
        if is_daily_rate(job.salary_text):
            return False
        if profile.is_daily_salary(job.salary_min_k, job.salary_max_k):
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
        # 记录详情成功获取的时间戳（活跃度是时效字段，抓完 30 天后就无意义了）
        from datetime import datetime, timezone
        job.detail_fetched_at = datetime.now(timezone.utc).isoformat()
        # 条内延迟用高斯分布（比 uniform 更像真人：有时快有时慢），2026-08-03 拟人化
        lo, hi = self.config.detail_delay_seconds
        # 越过日线后按倍数拉长（日线是降速线，见 over_quota_factor）。
        # 乘在**条内延迟**上而不是只乘批间冷却：条内延迟是每条都走的路径，
        # 乘它才能真正把速率压下去。
        f = self.over_quota_factor()
        await asyncio.sleep(gaussian(lo * f, hi * f))
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
                # 全保留分层：无效岗位跳过；画像外岗位也入库标 sample（去重样本），
                # 不再过滤丢弃，保证下次增量采集能识别为已见而 UPDATE 而非 INSERT。
                if not job.title or not job.job_url:
                    continue
                job.retention_status = "active" if self._should_keep(job) else "sample"
                # 去重前置：库里已有 JD 的岗位不再抓详情。13 个关键词的搜索结果
                # 大量交叉重叠，原先同一岗位每被搜到一次就重抓一次详情，白烧配额。
                already = job.canonical_key() in self.skip_detail_ids
                if already:
                    self.detail_skipped += 1
                if (
                    self.config.collect_details
                    and detailed < detail_limit
                    and job.retention_status == "active"
                    and not already
                ):
                    # 熔断（2026-08-10 补齐）：backfill 路径一直有 code37_limit=35，
                    # collect 路径完全没有——同一个 collector 类两条调用路径安全等级
                    # 不对称。code=37 本身可恢复，但每次重算都消耗信用，累积会升到
                    # code=32 封禁（实测：156 条仅 1 次 code37 就触发过 32）。
                    if self.code37_count >= self.code37_limit:
                        raise RiskControlStop(
                            f"code=37 重算 {self.code37_count} 次超阈值 "
                            f"{self.code37_limit}，主动停止防封禁"
                        )
                    try:
                        job = await self._collect_detail(job)
                        detailed += 1
                        # 本轮内同一岗位也可能再出现（换关键词），记下避免重复
                        self.skip_detail_ids.add(job.canonical_key())
                    except RiskControlStop:
                        # 必须原样上抛。RiskControlStop 继承 RuntimeError，原先被
                        # 下面的 `except Exception` 吞掉后只写进 detail_error 字段，
                        # 循环继续往下抓——真封禁时反而会持续撞墙加重惩罚。
                        raise
                    except Exception as exc:
                        job.raw["detail_error"] = str(exc)
                chat_status = await self._maybe_chat(job)
                if chat_status != "disabled":
                    job.raw["auto_chat_status"] = chat_status
                yield job
            if page_index < pages:
                low, high = self.config.request_delay_seconds
                await asyncio.sleep(random.uniform(low, high))
