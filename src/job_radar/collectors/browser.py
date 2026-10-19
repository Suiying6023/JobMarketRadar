from __future__ import annotations

from pathlib import Path

from patchright.async_api import Error as BrowserError, async_playwright

from ..local_files import file_mutex
from ..models import BrowserConfig, BrowserMode

# 窗口默认落点 `x,y`：主屏之外，跑批时不抢焦点。
DEFAULT_WINDOW_POSITION = "2400,1400"


def launch_args(window_position: str = DEFAULT_WINDOW_POSITION) -> list[str]:
    """浏览器启动参数。两条链路共用，别在调用处另拼一份。

    `--disable-blink-features=AutomationControlled` 是反检测必需项，
    同时也是 `cleanup_browsers.py` 认「这个浏览器是我们起的」的特征之一。

    ⚠️ **`window_position` 的坐标必须为正**。挑战 JS 把窗口几何当环境指纹：
    负的大数被判为异常环境，请求会被推去登录页（`/web/user/?seed=...`）而
    **不给可自解的挑战、也不给可点的验证页**——那种窗口人是点不动的。
    实测 `-32000,-32000` 得 0/4，正坐标 `2400,1400` 挑战率 7%（n=14），
    优于在屏的 14-16%（这两个数是窗口探针口径，只用于组间比较）。
    最小化同样不行（Windows 把坐标写成 -21333）。
    传空字符串 = 不指定位置，窗口开在屏幕中央会抢焦点。
    """
    args = ["--disable-blink-features=AutomationControlled"]
    if window_position:
        if window_position.startswith("-") or ",-" in window_position:
            raise ValueError(
                f"窗口坐标必须为正，收到 {window_position!r}。"
                "负坐标会让挑战 JS 判为异常环境，请求被推去登录页（实测 0/4）。")
        args.append(f"--window-position={window_position}")
    return args


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
                try:
                    await self.context.close()
                except BrowserError as exc:
                    # Closing the user login window already closes its persistent context.
                    if "Target page, context or browser has been closed" not in str(exc):
                        raise
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
