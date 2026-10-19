"""端到端测试底座：本地回放服务 + 真的会发 HTTP 的 Page 替身。

为什么不用假的返回值
--------------------
之前的 31 个测试文件、406 个用例，绝大多数是「手写一个 FakePage，让 evaluate 直接
返回一个 dict」。它验证的是"我传进去的 dict 能被解析"，**没有验证请求是怎么发出去的**：
header 拼错、状态码分支、非 JSON 正文、重定向、超时，全都不在覆盖范围内。
真实的漂移也是这么来的——生产代码加了 `page.on()`，替身没跟上，60 个用例一起红。

这里的做法：起一个真的 HTTP 服务（`http.server`），`ReplayPage.evaluate` 真的用
urllib 去请求它，把 `{status, text}` 原样返回。于是测试覆盖的是
**构造 URL → 发请求 → 读状态码 → 解码正文 → 判定风控 → 记账 → 解析入库** 整条链路。

代理必须绕开：本机 `HTTP_PROXY` 指向 Clash，本机回环请求走代理会拿到代理的错误页，
测试就会以一个和被测代码无关的原因失败（实测过一次，表现为 JSONDecodeError）。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from tests.fakes import PageEventRecorder

BOSS_ORIGIN = "https://www.zhipin.com"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
        server: ReplayServer = self.server.replay  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        server.calls.append({"path": path, "query": urlsplit(self.path).query,
                             "headers": dict(self.headers)})
        route = server.routes.get(path)
        if route is None:
            server.calls[-1]["matched"] = False
            body = json.dumps({"code": 404, "message": "no route"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        server.calls[-1]["matched"] = True
        status, payload = route["status"], route["payload"]
        if callable(payload):
            # 翻页要看 page 参数：同一路径不同 query 要给不同内容
            payload = payload(parse_qs(urlsplit(self.path).query))
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        else:
            body = str(payload).encode("utf-8")
            content_type = route.get("content_type") or "text/html; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class ReplayServer:
    """按路径回放的本地 HTTP 服务。每个路径可以单独设定状态码与正文。"""

    def __init__(self) -> None:
        self.routes: dict[str, dict] = {}
        self.calls: list[dict] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.replay = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def route(self, path: str, payload, *, status: int = 200,
              content_type: str | None = None) -> None:
        self.routes[path] = {"status": status, "payload": payload,
                             "content_type": content_type}

    def calls_to(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["path"] == path]

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


class ReplayPage(PageEventRecorder):
    """把 `https://www.zhipin.com/...` 重写到本地回放服务的 Page 替身。

    只实现采集器真正会调的那几项；缺一个就会由 `PageEventRecorder.__getattr__`
    抛出一条能直接照着修的错误，而不是一句光秃秃的 AttributeError。
    """

    def __init__(self, server: ReplayServer, *, url: str = f"{BOSS_ORIGIN}/web/geek/jobs") -> None:
        self.server = server
        self.url = url
        self.navigations: list[str] = []
        self.scripts: list[str] = []
        self.timeout = 5.0
        # 显式不继承系统代理：本机 HTTP_PROXY 指向 Clash，回环请求会被代理接管。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    async def evaluate(self, _script: str, arg=None):
        # 记下生产代码真正交给浏览器的脚本：请求头是写在这段 JS 里的，
        # Python 侧看不到，只有把脚本本身留档才能断言它。
        self.scripts.append(_script)
        if isinstance(arg, dict) and "url" in arg:
            return self._http(arg["url"])
        # 登录按钮检测：返回 False 表示页面上没有可见的登录入口
        return False

    def _http(self, url: str) -> dict:
        parts = urlsplit(url)
        target = f"{self.server.base_url}{parts.path}"
        if parts.query:
            target += f"?{parts.query}"
        try:
            with self._opener.open(target, timeout=self.timeout) as response:
                return {"status": response.status, "text": response.read().decode("utf-8")}
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "text": exc.read().decode("utf-8", "replace")}
        except Exception as exc:  # 连接被拒 / 超时
            return {"status": -1, "text": "", "error": str(exc)}

    async def goto(self, url: str, **_kw):
        """导航也走真实 HTTP —— 生产里导航本身就是一次平台请求（账本记 navigation）。

        如果这里只改个 url 不发请求，「账本行数 == 真实请求数」这条不变量在测试里
        就会假性成立，等于把最容易记漏的那一类请求排除在覆盖之外。
        """
        self.navigations.append(url)
        self.url = url
        result = self._http(url)
        return SimpleNamespace(status=result.get("status"))

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    def set_default_timeout(self, _ms: int) -> None:
        return None


class RefreshPage(ReplayPage):
    """刷新链路的 Page 替身：`evaluate` 真的按 `FETCH_JOB_JS` 的契约发请求。

    ## 为什么它必须真的发请求

    `refresh_one` 只做三件事：`page.evaluate(FETCH_JOB_JS, url)` 拿字节、
    `read_job_page` 解析、`classify` 判定。前两件在 Python 侧，第三件是纯函数。
    所以只要替身**真的把 URL 请求出去、把状态码和正文原样交回**，
    整条链（构造 URL → 真发 HTTP → 读状态码 → 解析 HTML → 判定 → 落库）
    就都在测试覆盖之内，且用的是回放服务里那份真 HTML。

    ## 验证页自愈是怎么模拟的

    站点在撞到验证页后，会靠自己那段 JS 把 URL 跳回 `job_detail/...?_security_check=1_<ts>`。
    真实实现里 `recover_session` 就是**轮询 `page.url` 等它自己跳回来**。
    这里把「跳回来」建模成：导航到验证页后，读 `url` 到第 `heal_after` 次时改回原地址。
    这样轮询逻辑本身（而不是某个 mock 的返回值）仍然是被测对象。
    """

    def __init__(self, server: ReplayServer, *,
                 url: str = f"{BOSS_ORIGIN}/job_detail/e2e000.html",
                 heal_after: int = 1) -> None:
        # 先铺好属性：`url` 是 property，getter 会读 `_pending_heal`，
        # 而基类 `__init__` 里就有赋值。
        self._pending_heal: str | None = None
        self._heal_reads = 0
        self._url = url
        self.heal_after = heal_after
        self.opened: list[RefreshPage] = []
        super().__init__(server, url=url)
        self.context = _FakeContext(self)

    @property
    def url(self) -> str:  # type: ignore[override]
        if self._pending_heal is not None:
            self._heal_reads += 1
            if self._heal_reads >= self.heal_after:
                self._url = self._pending_heal
                self._pending_heal = None
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    async def evaluate(self, script: str, arg=None):
        """`FETCH_JOB_JS` 的契约是「一个 URL 进，`{ok,status,url,text}` 出」。"""
        self.scripts.append(script)
        if not isinstance(arg, str):
            return False
        result = self._http(arg)
        if "error" in result:
            return {"ok": False, "reason": "fetch_error", "error": result["error"]}
        return {"ok": True, "status": result["status"], "url": arg,
                "text": result["text"]}

    async def goto(self, url: str, **_kw):
        """导航走真实 HTTP；如果落到了验证页，就把 `url` 改成验证页地址。"""
        self.navigations.append(url)
        result = self._http(url)
        body = result.get("text") or ""
        if "安全验证" in body or "verify.html" in body:
            self._url = f"{BOSS_ORIGIN}/web/passport/zp/verify.html"
            self._pending_heal = url
            self._heal_reads = 0
        else:
            self._url = url
        return SimpleNamespace(status=result.get("status"))

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeContext:
    """`context.new_page()` —— `recover_session` 靠它开一个干净页面。"""

    def __init__(self, owner: RefreshPage) -> None:
        self.owner = owner

    async def new_page(self) -> RefreshPage:
        page = RefreshPage(self.owner.server, url=self.owner.url,
                           heal_after=self.owner.heal_after)
        self.owner.opened.append(page)
        return page

    async def cookies(self, _urls) -> list:
        return []
