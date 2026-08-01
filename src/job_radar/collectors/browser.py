from __future__ import annotations

from pathlib import Path

from patchright.async_api import Browser, BrowserContext, Playwright, async_playwright

from ..models import BrowserConfig, BrowserMode


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
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.context.set_default_timeout(self.config.navigation_timeout_ms)
        self.owns_context = True
        return self.context

    async def close(self) -> None:
        if self.owns_context and self.context:
            await self.context.close()
        # CDP 模式只断开 Playwright，不关闭用户启动的浏览器。
        elif self.browser:
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
