"""按平台自己的算法滚动 `__zp_stoken__`。

## 为什么需要这个

2026-10-18 实测（已登录真实会话）：

* 同一个 `__zp_stoken__` 只允许 **5 个** 请求，第 6 个回 `code=37`「您的环境存在异常.」。
* 干等 30 秒 / 90 秒**都不恢复**；只有换掉 stoken 才恢复。
* 每个 `code=0` 响应都会下发新的种子三件套：

      __zp_sseed__  ← 种子（每次不同）
      __zp_sname__  ← 签名脚本名（同一部署内固定，本次 f45bbe80）
      __zp_sts__    ← 该响应的时间戳

* `code=37` 的响应**不下发任何 cookie**，所以"撞了再算"没有种子可用，必须**提前算**。

站点自己的前端就是这么做的：每个响应后重算 stoken，所以它永远不会撞 37。
我们以前不跑那段 JS，token 不滚动，于是 5 个请求后就撞墙，只能靠整页导航重铸
（一次 92~993 个真实请求）。这个模块把这件事按平台原算法补上，成本是 **0 个请求**。

## 算法（从站点 bundle `app_2` 的 module 49657 还原）

    __zp_stoken__ = new ABC().z(seed, parseInt(sts) + (480 + tzOffsetMin) * 60000)

`ABC` 由 `/web/common/security-js/<sname>.js` 定义，站点把它装在一个
`name="zhipinFrame"` 的 about:blank iframe 里。我们照做，只是**不删除种子**、
并把结果写回 cookie。

站点源码里的错误码把入参写死了，可作为交叉验证：
`gatewaySNTNull`「seed/name/ts 中有存在值为空」、
`gatewayABCNull`「ABC 不存在或者不是 1 个函数」。

## 三个必须踩准的细节

1. **cookie 值必须 URL 编码**。`ABC.z()` 返回标准 base64（含 `+` `/`），
   原样写入会被平台判为无效 —— 实测「编码 → code=0，不编码 → code=37」。
2. 种子是**会话 cookie**（无 Expires），进程退出即失效。冷启动必须靠一次
   整页导航 bootstrap，之后才能滚动。
3. **不能用 `page.evaluate` 直接读 `window.ABC`**。采集器用的是 patchright，
   它的 evaluate 跑在**隔离世界**，而注入的 `<script>` 在**主世界**执行，
   两边 window 是两个对象 —— 脚本明明跑了，读回来永远是 `undefined`
   （实测：同一份脚本 plain playwright 得 `function`，patchright 得 `undefined`，
   而且不报错）。DOM 是两个世界共享的，所以让主世界那段脚本把结果写到
   `data-zp-*` 属性上，再从隔离世界读属性。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import quote

STOKEN_COOKIE = "__zp_stoken__"
SEED_COOKIE = "__zp_sseed__"
NAME_COOKIE = "__zp_sname__"
TS_COOKIE = "__zp_sts__"
SEED_COOKIES = (SEED_COOKIE, NAME_COOKIE, TS_COOKIE)

# stoken 有效期：站点自己写的是 3840 秒，照抄。
STOKEN_TTL_SECONDS = 3840

SECURITY_JS_PATH = "/web/common/security-js/{sname}.js"

# 主世界脚本把每一步写到这些属性上，供隔离世界读取。
DATA_PREFIX = "data-zp-"

# 注入到主世界的脚本。**必须**通过 <script> 标签注入才能进主世界；
# 直接 page.evaluate 同一段代码会在隔离世界里跑，看不见 window.ABC。
_BRIDGE_SCRIPT = """
(async () => {
  const root = document.documentElement;
  const put = (key, value) => root.setAttribute('data-zp-' + key, String(value));
  put('state', 'start');
  try {
    const read = (name) => {
      const hit = document.cookie.split('; ').find((row) => row.startsWith(name + '='));
      return hit ? decodeURIComponent(hit.slice(name.length + 1)) : null;
    };
    const seed = read('__zp_sseed__');
    const sname = read('__zp_sname__');
    const sts = read('__zp_sts__');
    put('sname', sname || '');
    if (!seed || !sname || !sts) { put('state', 'no-seed'); return; }

    if (!window.__zpMintFrame) {
      const frame = document.createElement('iframe');
      frame.name = 'zhipinFrame';
      frame.src = 'about:blank';
      frame.style.cssText = 'height:0;width:0;border:0 none;position:fixed;z-index:-100';
      root.appendChild(frame);
      window.__zpMintFrame = frame;
    }
    const frame = window.__zpMintFrame;
    const url = '/web/common/security-js/' + sname + '.js';
    if (frame.__loadedScript !== url) {
      const doc = frame.contentDocument;
      const s = doc.createElement('script');
      s.type = 'text/javascript';
      const done = new Promise((resolve) => {
        s.onload = () => resolve('load');
        s.onerror = () => resolve('error');
        setTimeout(() => resolve('timeout'), 15000);
      });
      s.src = url;
      (doc.body || doc.documentElement).appendChild(s);
      const how = await done;
      put('script', how);
      if (how !== 'load') { put('state', 'script-' + how); return; }
      frame.__loadedScript = url;
    }
    const ABC = frame.contentWindow.ABC;
    put('abc', typeof ABC);
    if (typeof ABC !== 'function') { put('state', 'no-abc'); return; }

    const tz = new Date().getTimezoneOffset();
    const token = new ABC().z(seed, parseInt(sts) + (480 + tz) * 60000);
    put('token', token || '');
    put('state', token ? 'ok' : 'empty');
  } catch (e) {
    put('state', 'error');
    put('error', String((e && e.message) || e).slice(0, 180));
  }
})();
"""

_INJECT_JS = """(source) => {
  const s = document.createElement('script');
  s.textContent = source;
  (document.head || document.documentElement).appendChild(s);
  return true;
}"""

_READ_JS = """() => {
  const root = document.documentElement;
  const get = (key) => root.getAttribute('data-zp-' + key);
  return {state: get('state'), sname: get('sname'), script: get('script'),
          abc: get('abc'), token: get('token'), error: get('error')};
}"""

_CLEAR_JS = """() => {
  const root = document.documentElement;
  for (const key of ['state', 'sname', 'script', 'abc', 'token', 'error']) {
    root.removeAttribute('data-zp-' + key);
  }
  return true;
}"""

_TERMINAL_STATES = {"ok", "no-seed", "no-abc", "empty", "error",
                    "script-error", "script-timeout"}


@dataclass
class RollResult:
    rolled: bool
    reason: str = ""
    sname: str | None = None
    token_length: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.rolled


class StokenRoller:
    """在给定的浏览器 context 上滚动 stoken。

    需要一个**同源宿主页**来执行签名脚本（脚本要按 `/web/...` 相对路径加载）。
    默认用 `/robots.txt`：同源、极小、不会被 SPA 路由接管、也不会自己发接口请求。
    """

    def __init__(self, context, *, origin: str, host_path: str = "/robots.txt",
                 poll_interval: float = 0.1, poll_timeout: float = 20.0) -> None:
        self.context = context
        self.origin = origin.rstrip("/")
        self.host_path = host_path
        self.host = None
        self.roll_count = 0
        self.fail_count = 0
        self.last_error: str | None = None
        self.last_state: str | None = None
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    async def start(self) -> None:
        """建宿主页。失败不致命——没有宿主页时 roll() 会返回 not-ready。"""
        if self.host is not None and not self.host.is_closed():
            return
        self.host = await self.context.new_page()
        try:
            await self.host.goto(f"{self.origin}{self.host_path}",
                                 wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001 - 宿主页不可用只降级，不中断采集
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]

    async def close(self) -> None:
        host, self.host = self.host, None
        if host is not None:
            try:
                await host.close()
            except Exception:  # noqa: BLE001 - 关页失败不影响上层收尾
                pass

    async def _seeds(self) -> dict[str, str]:
        cookies = await self.context.cookies([f"{self.origin}/"])
        return {cookie["name"]: cookie["value"] for cookie in cookies}

    async def roll(self) -> RollResult:
        """读种子 → 算 stoken → 写回 cookie。

        调用点应该在**每次成功的接口响应之后**：响应已经把新种子放进 cookie jar，
        这里把它变成下一个请求要用的 stoken。
        """
        if self.host is None or self.host.is_closed():
            await self.start()
        if self.host is None or self.host.is_closed():
            self.fail_count += 1
            self.last_error = self.last_error or "宿主页不可用"
            return RollResult(False, "no-host", error=self.last_error)

        # 先在 Python 侧确认种子齐全：缺了就不必往页面里跑一趟，
        # 而且能明确区分"没种子"和"算失败"。
        jar = await self._seeds()
        missing = [name for name in SEED_COOKIES if not jar.get(name)]
        if missing:
            self.fail_count += 1
            self.last_error = f"缺种子: {','.join(missing)}"
            self.last_state = "no-seed"
            return RollResult(False, "no-seed", error=self.last_error)

        try:
            await self.host.evaluate(_CLEAR_JS)
            await self.host.evaluate(_INJECT_JS, _BRIDGE_SCRIPT)
        except Exception as exc:  # noqa: BLE001 - 页面没了就降级
            self.fail_count += 1
            self.last_error = f"注入失败: {type(exc).__name__}: {exc}"[:160]
            return RollResult(False, "inject-failed", error=self.last_error)

        state: dict = {}
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_interval)
            try:
                state = await self.host.evaluate(_READ_JS) or {}
            except Exception as exc:  # noqa: BLE001
                self.fail_count += 1
                self.last_error = f"读取失败: {type(exc).__name__}: {exc}"[:160]
                return RollResult(False, "read-failed", error=self.last_error)
            if state.get("state") in _TERMINAL_STATES:
                break

        self.last_state = state.get("state")
        sname = state.get("sname") or jar.get(NAME_COOKIE)
        token = state.get("token")
        if state.get("state") != "ok" or not token:
            self.fail_count += 1
            self.last_error = (state.get("error")
                               or f"状态={state.get('state')} abc={state.get('abc')} "
                                  f"script={state.get('script')}")
            return RollResult(False, self.last_state or "unknown", sname=sname,
                              error=self.last_error[:200])

        # ⚠️ 必须 URL 编码。base64 里的 + / 原样写进 cookie 会被平台判无效。
        await self.context.add_cookies([{
            "name": STOKEN_COOKIE,
            "value": quote(token, safe=""),
            "domain": ".zhipin.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "expires": time.time() + STOKEN_TTL_SECONDS,
        }])
        self.roll_count += 1
        self.last_error = None
        return RollResult(True, "rolled", sname=sname, token_length=len(token))


async def roll_quietly(roller: StokenRoller | None) -> RollResult:
    """滚动失败不该让采集停下——37 还有兜底。异常收敛成 RollResult。"""
    if roller is None:
        return RollResult(False, "disabled")
    try:
        return await roller.roll()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 滚动是优化项，不是正确性依赖
        return RollResult(False, "exception", error=f"{type(exc).__name__}: {exc}"[:160])
