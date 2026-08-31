from __future__ import annotations

from pathlib import Path

from patchright.async_api import Browser, BrowserContext, Playwright, async_playwright

from ..models import BrowserConfig, BrowserMode
from ..refresh_fetch import launch_args


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.owns_context = False

    async def start(self) -> BrowserContext:
        if self.config.mode is BrowserMode.CDP:
            # 兼容旧配置：patchright 不支持 connect_over_cdp（连调试端口本身会被 BOSS
            # 前端检测触发刷新循环），这里退回标准 playwright 的 connect_over_cdp。
            # 推荐改用 PERSISTENT 模式（见下）。
            import playwright.async_api as pw_api

            pw = await pw_api.async_playwright().start()
            self.playwright = pw  # type: ignore[assignment]
            try:
                self.browser = await pw.chromium.connect_over_cdp(
                    self.config.cdp_url,
                    slow_mo=self.config.slow_mo_ms,
                    timeout=self.config.navigation_timeout_ms,
                )
            except Exception:
                await pw.stop()
                raise
            if not self.browser.contexts:
                raise RuntimeError("CDP 浏览器没有可用 context")
            self.context = self.browser.contexts[0]
            return self.context

        # PERSISTENT 模式（推荐）：用 patchright 直接启动 Chrome，复用指定 profile
        # 的登录态，不连调试端口，规避 BOSS 的端口扫描 + Runtime.enable 检测。
        self.playwright = await async_playwright().start()
        profile = Path(self.config.user_data_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(profile),
            channel=self.config.channel,
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
            no_viewport=True,
            # 窗口落点见 `BrowserConfig.window_position`（坐标必须为正，那是指纹面）。
            args=launch_args(self.config.window_position),
            ignore_default_args=["--enable-automation"],
        )
        self.context.set_default_timeout(self.config.navigation_timeout_ms)
        await self._block_heavy_resources(self.context)
        self.owns_context = True
        return self.context

    @staticmethod
    async def _block_heavy_resources(context: BrowserContext) -> None:
        """拦掉图片/字体/媒体与埋点域。**只影响导航，不影响同源 fetch**。

        为什么必须有（2026-08-30 实测）：采集链路的**每次 stoken 重算发 226 次
        HTTP 请求**（两次整页导航：`security-check.html` + 回搜索页），
        而账本只记 1 行 —— 低估 226 倍。按 08-29 那轮（1019 条详情、
        182 次重算）折算，真实请求量约 42151 次而非账本上的 1201 次，
        即**每条详情 41.4 次**而不是 1.18 次。

        这条链路一直没有任何 `page.route` 拦截，所以那两次导航把
        `static.zhipin.com`(98)、`img.bosszhipin.com`(45) 等子资源全下载了。
        刷新链路早就在拦（一条 102 → 约 10 次），这里照搬同一套判据。

        ⚠️ 绝不能拦 `script` / `document` / `xhr`：
        stoken 靠自家 JS（`security-js/<name>.js`）计算，拦掉脚本
        等于让重算永远算不出来；而 `xhr` 就是采集本身要读的接口。
        ⚠️ `logapi.zhipin.com` 是 BOSS 自己的埋点上报，拦掉同时少交行为特征。
        """
        blocked_types = {"image", "media", "font"}
        blocked_hosts = (
            "logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
            "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com",
            "img.bosszhipin.com",
        )

        async def _route(route) -> None:
            req = route.request
            host = (req.url.split("/")[2] if "//" in req.url else "").lower()
            if req.resource_type in blocked_types or any(
                b in host for b in blocked_hosts
            ):
                await route.abort()
            else:
                await route.continue_()

        # 挂在 context 上而不是单个 page：采集会开新页签（_maybe_chat），
        # 挂 page 级的话新页签不受保护。
        await context.route("**/*", _route)

    async def close(self) -> None:
        if self.owns_context and self.context:
            await self.context.close()
        # CDP 模式只断开 Playwright，不关闭用户启动的浏览器。
        elif self.browser:
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
