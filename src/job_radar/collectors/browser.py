from __future__ import annotations

from pathlib import Path

from patchright.async_api import async_playwright

from ..local_files import file_mutex
from ..models import BrowserConfig, BrowserMode
from ..refresh_fetch import launch_args


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context = None
        self.owns_context = False
        self._profile_guard = None

    async def start(self):
        if self.config.mode is BrowserMode.CDP:
            from playwright.async_api import async_playwright as standard_playwright
            self.playwright = await standard_playwright().start()
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(
                    self.config.cdp_url, slow_mo=self.config.slow_mo_ms,
                    timeout=self.config.navigation_timeout_ms)
                if not self.browser.contexts:
                    raise RuntimeError("连接的浏览器没有可用会话")
                self.context = self.browser.contexts[0]
                return self.context
            except BaseException:
                await self.close()
                raise
        profile = Path(self.config.user_data_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._profile_guard = file_mutex(profile.with_name(profile.name + ".service.lock"))
        try:
            self._profile_guard.__enter__()
        except OSError:
            self._profile_guard = None
            raise RuntimeError("此浏览器会话正被另一个任务使用")
        try:
            self.playwright = await async_playwright().start()
            proxy = None
            if self.config.proxy_server:
                proxy = {"server": self.config.proxy_server.replace("socks5h://", "socks5://")}
                if self.config.proxy_username:
                    proxy["username"] = self.config.proxy_username
                if self.config.proxy_password:
                    proxy["password"] = self.config.proxy_password
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), channel=self.config.channel, headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms, no_viewport=True, proxy=proxy,
                args=launch_args(self.config.window_position) + ([] if proxy else ["--no-proxy-server"]),
                ignore_default_args=["--enable-automation"])
            self.owns_context = True
            self.context.set_default_timeout(self.config.navigation_timeout_ms)
            if self.config.block_heavy_resources:
                await self._block_heavy_resources(self.context)
            return self.context
        except BaseException:
            await self.close()
            raise

    @staticmethod
    async def _block_heavy_resources(context):
        async def route_handler(route):
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", route_handler)

    async def show(self, page):
        if self.context and self.owns_context:
            await self.context.unroute("**/*")
        await page.bring_to_front()
        # Bring a user-requested verification window onto the visible desktop.
        if self.context:
            session = None
            try:
                session = await self.context.new_cdp_session(page)
                info = await session.send("Browser.getWindowForTarget")
                await session.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"windowState": "normal"}})
                await session.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"left": 80, "top": 60, "width": 1100, "height": 800}})
            finally:
                if session:
                    await session.detach()

    async def close(self):
        try:
            if self.owns_context and self.context:
                await self.context.close()
        finally:
            try:
                if self.playwright:
                    await self.playwright.stop()
            finally:
                self.context = self.browser = self.playwright = None
                self.owns_context = False
                if self._profile_guard:
                    self._profile_guard.__exit__(None, None, None)
                    self._profile_guard = None
