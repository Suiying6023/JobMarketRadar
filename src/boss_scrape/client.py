"""抓取客户端：一个类管住浏览器、节奏、token 滚动和风控分类。

设计上只做一件事——**把 BOSS 的接口响应稳定地拿回来**。
存储、过滤、画像、去重全部留给调用方。

## 一次请求的完整生命周期

    限速等待 → 同源 fetch → 判定 HTTP → 解析 JSON → 判定 code
                                                   ├─ 0   → 就地滚 token，返回
                                                   ├─ 37  → 滚 token 后重试一次
                                                   ├─ 36  → 抛 VerificationRequired
                                                   ├─ 32/35 → 抛 AccountRestricted
                                                   └─ 7   → 抛 LoginExpired

"就地滚 token" 是整个设计的支点：一个 stoken 只给 5 个请求，
但每个成功响应都会下发新种子，算新 token 的成本是 **0 个请求**。
不滚就得整页导航（一次 92~993 个请求）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .errors import (AccountRestricted, LoginExpired, RiskControl, Throttled,
                     TokenWindowExhausted, VerificationRequired)
from .models import ORIGIN, JobCard, JobDetail, SearchSpec, parse_card, parse_detail
from .runlog import RunLog
from .seed_store import SeedStore
from .stoken import StokenRoller

LIST_PATH = "/wapi/zpgeek/search/joblist.json"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
USER_INFO_PATH = "/wapi/zpuser/wap/getUserInfo.json"
SEARCH_PAGE = "/web/geek/jobs"

# 风控响应里出现这些字样就按"验证页"处理，不要当成格式错误。
_VERIFY_HINTS = ("verify.html", "safe-validate", "安全验证", "请完成验证", "完成验证")
# 「访问频繁」是限速，不是封号。code 仍然是 0。
_THROTTLE_HINTS = ("访问频繁", "操作过于频繁", "操作太频繁", "请稍后再试")
# 日志里最多留这么长的响应正文，够判断是验证页还是错误体，又不至于把 JD 写进去。
MAX_LOG_BODY = 400

_FETCH_JS = """async ([url, headers]) => {
  try {
    const response = await fetch(url, {credentials: 'include', headers: headers});
    const text = await response.text();
    return {status: response.status, text: text};
  } catch (e) {
    return {error: String((e && e.message) || e)};
  }
}"""


@dataclass(slots=True)
class Stats:
    requests: int = 0
    rolls: int = 0
    # 响应里没有种子（`getUserInfo` 这类接口本来就不下发）——不是失败。
    roll_skipped: int = 0
    roll_failures: int = 0
    throttled: int = 0
    retried_37: int = 0

    def as_dict(self) -> dict:
        return {"requests": self.requests, "rolls": self.rolls,
                "roll_skipped": self.roll_skipped,
                "roll_failures": self.roll_failures,
                "throttled": self.throttled, "retried_37": self.retried_37}


class BossClient:
    def __init__(self, *, profile: str | Path,
                 headless: bool = False,
                 delay: tuple[float, float] = (4.0, 8.0),
                 burst_every: int = 25,
                 burst_pause: tuple[float, float] = (40.0, 70.0),
                 block_heavy: bool = True,
                 window_position: str = "80,60",
                 navigation_timeout_ms: int = 60_000,
                 origin: str = ORIGIN,
                 log_path: str | Path | None = None,
                 seed_path: str | Path | None = None) -> None:
        self.profile = str(Path(profile).resolve())
        self.headless = headless
        self.delay = delay
        self.burst_every = max(1, burst_every)
        self.burst_pause = burst_pause
        self.block_heavy = block_heavy
        self.window_position = window_position
        self.navigation_timeout_ms = navigation_timeout_ms
        self.origin = origin.rstrip("/")
        # 每次真实运行都落一份样本，用来事后标定风控阈值（见 runlog.py）。
        self.log = RunLog(log_path)
        # 种子落盘：让下一次冷启动不必再加载整页（实测一个文档请求就够）。
        self.seed_store = SeedStore(seed_path)
        self._restored = False

        self._playwright = None
        self._context = None
        self.page = None
        self.roller: StokenRoller | None = None
        self.stats = Stats()
        self._since_burst = 0

    # ---------------------------------------------------------------- 生命周期

    async def __aenter__(self) -> BossClient:
        await self.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def open(self) -> None:
        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-session-crashed-bubble", "--no-first-run",
                f"--window-position={self.window_position}",
                "--window-size=1120,860"]
        if not self.block_heavy:
            args.append("--disable-features=CalculateNativeWinOcclusion")
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.profile, channel="chrome", headless=self.headless,
                no_viewport=True, args=args,
                ignore_default_args=["--enable-automation"])
        except BaseException:
            await self.close()
            raise
        self._context.set_default_timeout(self.navigation_timeout_ms)
        if self.block_heavy:
            # 实测：拦掉 image/font/media 后一次页面加载的请求数从 993 降到 92，
            # 而且不影响 stoken 重铸的有效性。
            await self._context.route("**/*", self._route)

        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.roller = StokenRoller(self._context, origin=self.origin)
        await self.roller.start()
        self.log.write("start", headless=self.headless, block_heavy=self.block_heavy,
                       delay=list(self.delay), burst_every=self.burst_every,
                       roller_host_error=self.roller.last_error)

        # 有落盘状态就恢复 cookie，直接拿宿主页（/robots.txt）发请求——
        # 一个文档请求换掉一次整页加载（实测整页 92~993 个请求）。
        # 状态过期时服务端会回 37，`_get` 里会退回整页 bootstrap。
        self._restored = await self._restore_state()
        if self._restored:
            await self._adopt_host_page()
            self.log.write("restore", age_s=round(self.seed_store.age_seconds() or -1, 1))
        else:
            self.log.write("bootstrap", reason=self.seed_store.last_error or "no-state")
            await self.bootstrap()
        await self._persist_state()

    async def _restore_state(self) -> bool:
        state = self.seed_store.load()
        if not state:
            return False
        await self._context.add_cookies([
            {"name": name, "value": value, "domain": ".zhipin.com", "path": "/",
             "secure": True, "httpOnly": False, "expires": time.time() + 3840}
            for name, value in state.items()])
        return True

    async def _persist_state(self) -> None:
        if not self.seed_store.enabled or self._context is None:
            return
        try:
            jar = {cookie["name"]: cookie["value"]
                   for cookie in await self._context.cookies([f"{self.origin}/"])}
        except Exception:  # noqa: BLE001 - 读不到就不存，不影响抓取
            return
        self.seed_store.save(jar)

    async def _adopt_host_page(self) -> None:
        """把宿主页当成请求页，并关掉那个没用的空白页。"""
        host = self.roller.host if self.roller else None
        if host is None:
            return
        previous, self.page = self.page, host
        if previous is not None and previous is not host:
            try:
                await previous.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        self.log.write("end", **self.stats.as_dict())
        try:
            if self.roller is not None:
                await self.roller.close()
        finally:
            try:
                if self._context is not None:
                    await self._context.close()
            finally:
                try:
                    if self._playwright is not None:
                        await self._playwright.stop()
                finally:
                    self._context = self.page = self.roller = None
                    self._playwright = None

    @staticmethod
    async def _route(route) -> None:
        if route.request.resource_type in {"image", "font", "media"}:
            await route.abort()
        else:
            await route.continue_()

    async def show_window(self) -> None:
        """把浏览器窗口推到前台。需要人工过验证时调它。"""
        if self.page is None:
            return
        await self.page.bring_to_front()
        session = None
        try:
            session = await self._context.new_cdp_session(self.page)
            info = await session.send("Browser.getWindowForTarget")
            await session.send("Browser.setWindowBounds", {
                "windowId": info["windowId"],
                "bounds": {"windowState": "normal", "left": 80, "top": 60,
                           "width": 1120, "height": 860}})
        except Exception:  # noqa: BLE001 - 置前失败不影响抓取
            pass
        finally:
            if session is not None:
                await session.detach()

    # ---------------------------------------------------------------- 启动

    def _search_url(self, query: str = "AI Agent", city: str = "101210100") -> str:
        return (f"{self.origin}{SEARCH_PAGE}?query={query.replace(' ', '%20')}"
                f"&city={city}")

    async def bootstrap(self, *, query: str = "AI Agent", city: str = "101210100") -> None:
        """一次整页加载，建立有效 stoken。

        种子是**会话 cookie**，进程重启就没了，所以每次冷启动都要走这一趟。
        顺带确认页面不是验证页——是的话直接抛，别继续发请求。
        """
        if self.page is None:
            raise RiskControl("浏览器页面不可用")
        await self.page.goto(self._search_url(query, city),
                             wait_until="domcontentloaded")
        await self.page.wait_for_timeout(4000)
        await self._assert_not_blocked(await self._page_html())

    async def _page_html(self) -> str:
        try:
            return await self.page.content()
        except Exception:  # noqa: BLE001 - 导航中读不到属正常
            return ""

    async def _assert_not_blocked(self, html: str) -> None:
        url = self.page.url if self.page else ""
        if any(hint in url for hint in _VERIFY_HINTS) or \
                any(hint in (html or "") for hint in _VERIFY_HINTS):
            raise VerificationRequired(
                "平台要求人工过验证（页面被带到 verify.html）。"
                "等待无效、重载也无效，请调用 show_window() 让人过完验证。")

    # ---------------------------------------------------------------- 请求

    async def _pace(self) -> None:
        self._since_burst += 1
        if self._since_burst > self.burst_every:
            self._since_burst = 1
            low, high = self.burst_pause
            await asyncio.sleep(random.uniform(low, high))
            return
        low, high = self.delay
        await asyncio.sleep(random.uniform(low, high))

    async def _ensure_origin(self) -> None:
        if self.page is None:
            raise RiskControl("浏览器页面不可用")
        if urlsplit(self.page.url or "").netloc == "www.zhipin.com":
            return
        await self.bootstrap()

    async def _get(self, path: str, *, retried_37: bool = False,
                   retried_throttle: bool = False,
                   fell_back: bool = False) -> dict:
        await self._ensure_origin()
        await self._pace()
        url = path if path.startswith("http") else f"{self.origin}{path}"
        if urlsplit(url).netloc != "www.zhipin.com":
            raise ValueError("只允许向 BOSS 同源接口发请求")

        started = time.monotonic()
        result = await self.page.evaluate(_FETCH_JS, [url, {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.origin}{SEARCH_PAGE}",
        }])
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self.stats.requests += 1
        if result.get("error"):
            self.log.write("request", path=path, error=str(result["error"])[:160],
                           ms=elapsed_ms, requests=self.stats.requests)
            raise RiskControl(f"同源请求失败: {result['error']}")

        status = int(result.get("status") or 0)
        text = result.get("text") or ""
        if status in (401, 403, 429):
            self.log.write("request", path=path, http=status, ms=elapsed_ms,
                           requests=self.stats.requests)
            self.log.write("risk", path=path, http=status, code=None,
                           body=text[:200])
            raise RiskControl(f"接口返回 HTTP {status}", status=status)

        try:
            body = json.loads(text or "{}")
        except json.JSONDecodeError:
            # 非 JSON 往往是验证页 HTML —— 记下正文片段，这是判断"是不是要人工验证"的原始证据。
            self.log.write("request", path=path, http=status, code=None,
                           non_json=True, ms=elapsed_ms, requests=self.stats.requests,
                           body=text[:MAX_LOG_BODY])
            await self._assert_not_blocked(text)
            raise RiskControl(f"接口返回非 JSON（HTTP {status}）: {text[:120]}") from None
        if not isinstance(body, dict):
            raise RiskControl("接口返回了非对象 JSON")

        code = body.get("code")
        message = str(body.get("message") or "")
        self.log.write("request", path=path, http=status, code=code,
                       message=message[:80], ms=elapsed_ms,
                       requests=self.stats.requests, rolls=self.stats.rolls)

        if code == 0:
            if any(hint in message for hint in _THROTTLE_HINTS):
                self.stats.throttled += 1
                self.log.write("throttle", path=path, message=message[:80],
                               retry=not retried_throttle)
                if not retried_throttle:
                    await asyncio.sleep(random.uniform(20.0, 40.0))
                    return await self._get(path, retried_37=retried_37,
                                           retried_throttle=True)
                raise Throttled(message, retry_after=30.0)
            await self._roll()
            return body

        if code == 37:
            # 设计内的轮换：换 token 再来一次。换过还不行才是真异常。
            rolled = await self._roll() if not retried_37 else False
            if not retried_37 and rolled:
                self.stats.retried_37 += 1
                self.log.write("risk", path=path, code=37, handled="retry_after_roll",
                               message=message[:80])
                return await self._get(path, retried_37=True,
                                       retried_throttle=retried_throttle)
            # 落盘状态过期时，唯一的出路是重新整页加载拿一套新种子。
            # 只退一次，避免"恢复→过期→bootstrap→过期"来回打转。
            if self._restored and not fell_back:
                self.log.write("risk", path=path, code=37, handled="state_stale",
                               message=message[:80])
                self._restored = False
                self.seed_store.clear()
                await self.bootstrap()
                await self._persist_state()
                return await self._get(path, retried_37=True,
                                       retried_throttle=retried_throttle,
                                       fell_back=True)
            self.log.write("risk", path=path, code=37, handled="stop",
                           message=message[:80])
            raise TokenWindowExhausted(
                f"换了 token 仍被拒：{message or 'code=37'}", code=37, status=status)

        if code == 36:
            self.log.write("risk", path=path, code=36, handled="human_verification",
                           message=message[:80])
            raise VerificationRequired(
                f"平台要求人工过验证：{message or 'code=36'}。"
                "实测等待 12 分钟无效、整页重载无效，只有人点一下能解。")

        if code in (32, 35):
            self.log.write("risk", path=path, code=code, handled="banned",
                           message=message[:80])
            raise AccountRestricted(
                f"账号/出口被封：{message or f'code={code}'}", code=code)

        if code == 7:
            self.log.write("risk", path=path, code=7, handled="login_expired",
                           message=message[:80])
            raise LoginExpired(f"登录态失效：{message or 'code=7'}")

        self.log.write("risk", path=path, code=code, handled="unexpected",
                       message=message[:80])
        raise RiskControl(f"未预期的响应 code={code}: {message[:80]}",
                          code=code if isinstance(code, int) else None, status=status)

    async def _roll(self) -> bool:
        if self.roller is None:
            return False
        result = await self.roller.roll()
        # 只记结果和脚本名，不记 token/种子本身——那是凭据，不是样本。
        self.log.write("roll", ok=bool(result), reason=result.reason,
                       sname=result.sname, token_len=result.token_length,
                       error=(result.error or "")[:160] or None)
        if result:
            self.stats.rolls += 1
            await self._persist_state()
            return True
        # 没有种子 ≠ 失败：`getUserInfo` 这类接口本来就不下发种子，
        # 硬算成失败会让统计失真，也会掩盖真正的"该有种子却没有"。
        if result.reason == "no-seed":
            self.stats.roll_skipped += 1
            return False
        self.stats.roll_failures += 1
        return False

    # ---------------------------------------------------------------- 业务接口

    async def user_id(self) -> int | None:
        """确认登录态。返回 userId；未登录返回 None。"""
        body = await self._get(USER_INFO_PATH)
        data = body.get("zpData")
        value = data.get("userId") if isinstance(data, dict) else None
        return value if isinstance(value, int) and value > 0 else None

    async def search(self, query: str, *, city: str = "101210100",
                     pages: int = 1, page_size: int = 30,
                     filters: dict[str, str] | None = None) -> AsyncIterator[JobCard]:
        """按页产出岗位卡片。同一轮里复用 `lid`，更接近真实翻页。"""
        spec = SearchSpec(query=query, city=city, pages=pages,
                          page_size=page_size, filters=dict(filters or {}))
        lid = ""
        for page_index in range(1, pages + 1):
            params = spec.params(page_index, lid)
            body = await self._get(f"{LIST_PATH}?{urlencode(params)}")
            data = body.get("zpData")
            if not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
                raise RiskControl("列表响应结构不完整")
            lid = str(data.get("lid") or lid)
            jobs = data["jobList"]
            if not jobs:
                return
            for rank, raw in enumerate(jobs, start=1):
                yield parse_card(raw, keyword=query, rank=rank)
            if data.get("hasMore") is False:
                return

    async def detail(self, card: JobCard) -> JobDetail:
        """抓详情。`securityId` 会被平台轮换，用卡片上的最新值。"""
        if not card.security_id:
            raise ValueError("岗位缺少 securityId，无法请求详情")
        params = {"securityId": card.security_id}
        if card.lid:
            params["lid"] = card.lid
        body = await self._get(f"{DETAIL_PATH}?{urlencode(params)}")
        data = body.get("zpData")
        if not isinstance(data, dict):
            raise RiskControl("详情响应结构不完整")
        return parse_detail(data, job_id=card.job_id)
