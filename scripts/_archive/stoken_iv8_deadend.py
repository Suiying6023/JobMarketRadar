"""__zp_stoken__ 本地计算（iv8 方案，逆向自 Yang-hua6/boss-）。

机制（2026-08-01 逆向确认）：
  BOSS 对每个会话下发 seed/ts/name（在 code=37 响应的 zpData 里），__zp_stoken__
  是「seed+ts」的短时效签名，由 security-js/{name}.js 里的 (new window.ABC).z(seed, ts)
  计算。stoken 过期后请求返回 code=37，需要用响应里的新 seed 重新计算。

本模块用 iv8（内嵌 V8 引擎的 Python 库）本地执行签名 JS，无需浏览器。

用法：
    from job_radar.stoken import compute_stoken
    token = compute_stoken(seed, ts, name, session)  # session 需支持 get()
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse

log = logging.getLogger(__name__)

# iv8 静默导入：它的初始化会往 stdout 打横幅，包起来不污染日志
_IV8_AVAILABLE = False
try:
    _devnull = os.open(os.devnull, os.O_WRONLY)
    _stdout_fd = os.dup(1)
    _stderr_fd = os.dup(2)
    try:
        os.dup2(_devnull, 1)
        os.dup2(_devnull, 2)
        import iv8 as _iv8
        _IV8_AVAILABLE = True
    finally:
        os.dup2(_stdout_fd, 1)
        os.dup2(_stderr_fd, 2)
        os.close(_stdout_fd)
        os.close(_stderr_fd)
        os.close(_devnull)
except Exception:
    pass

if not _IV8_AVAILABLE:
    log.warning("iv8 未安装，无法本地计算 stoken。请执行: pip install iv8")

# Canvas 指纹（固定值，iv8 沙箱环境用，参考项目同款）
_CANVAS_PNG = (
    "data:image/png;base64,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"
)

# JS 文件内存缓存（同进程内不重复下载）
_js_cache: dict[str, str] = {}


def _fetch_js(name: str, session) -> str:
    """下载 security-js/{name}.js，带进程内缓存。

    兼容 requests 与 urllib：requests 的 Response 有 raise_for_status，
    urllib 的 HTTPResponse 没有，这里统一处理，不依赖调用方类型。
    """
    if name in _js_cache:
        return _js_cache[name]
    url = f"https://www.zhipin.com/web/common/security-js/{name}.js"
    resp = session.get(url, timeout=15)
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    text = resp.text if hasattr(resp, "text") else resp.read().decode("utf-8", errors="replace")
    _js_cache[name] = text
    return text


def _compute_iv8(seed: str, ts: int, js_code: str, js_url: str, security_url: str) -> str | None:
    if not _IV8_AVAILABLE:
        return None
    try:
        environment = {
            "location": {
                "href": security_url,
                "origin": "https://www.zhipin.com",
                "protocol": "https:",
                "host": "www.zhipin.com",
                "hostname": "www.zhipin.com",
                "port": "",
                "pathname": "/web/common/security-check.html",
                "search": "?" + security_url.split("?", 1)[1] if "?" in security_url else "",
                "hash": "",
            },
            "canvas": {"fingerprint": {"toDataURL": {"png": _CANVAS_PNG}}},
            "window": {"origin": "https://www.zhipin.com"},
        }
        html_page = (
            "<!DOCTYPE html><html><head></head><body>"
            f'<script src="{js_url}"></script>'
            "</body></html>"
        )
        with _iv8.JSContext(
            environment=environment,
            config={"timezone": "Asia/Shanghai"},
        ) as ctx:
            ctx.expose(
                {
                    "baseURL": environment["location"]["href"],
                    "html": html_page,
                    "headers": [],
                    "resources": {js_url: js_code},
                },
                "snapshot",
            )
            ctx.eval("__iv8__.page.load(__iv8__.data.snapshot)")
            seed_escaped = json.dumps(seed, ensure_ascii=False)
            token = ctx.eval(f"(new window.ABC).z({seed_escaped}, {int(ts)});")
        return str(token)
    except Exception as exc:
        log.warning(f"iv8 计算失败: {exc}")
        return None


def compute_stoken(seed: str, ts: int, name: str, session) -> str | None:
    """用 iv8 计算 __zp_stoken__，返回 token 字符串。

    session 需有 .get(url, timeout=...)（urllib/requests 均兼容）。
    """
    if not _IV8_AVAILABLE:
        log.error("iv8 不可用")
        return None
    try:
        js_url = f"https://www.zhipin.com/web/common/security-js/{name}.js"
        js_code = _fetch_js(name, session)
        security_url = (
            f"https://www.zhipin.com/web/common/security-check.html"
            f"?seed={urllib.parse.quote(str(seed))}&name={name}&ts={ts}"
            f"&callbackUrl=&srcReferer"
        )
        token = _compute_iv8(seed, int(ts), js_code, js_url, security_url)
        if token:
            log.info(f"stoken 计算成功: {token[:12]}...")
        return token
    except Exception as exc:
        log.error(f"compute_stoken 异常: {exc}")
        return None


def parse_code37(res_json: dict) -> tuple[str | None, int | None, str | None]:
    """从 code=37 响应里提取 (seed, ts, name)。"""
    zp = res_json.get("zpData") or {}
    return zp.get("seed"), zp.get("ts"), zp.get("name")
