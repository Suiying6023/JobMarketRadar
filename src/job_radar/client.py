"""Loopback JSON client used by Agent-facing CLI commands."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener


class ServiceClient:
    def __init__(self, base_url="http://127.0.0.1:8765"):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("服务地址使用本机 HTTP 主机与端口")
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, payload=None):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            state = self.request("/api/state")
            headers.update({"Content-Type": "application/json", "X-CSRF-Token": state["csrf_token"]})
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            with self.opener.open(Request(self.base_url+path, data=data, headers=headers), timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "服务请求失败")
            except (ValueError, AttributeError):
                detail = "服务请求失败"
            raise ValueError(f"HTTP {exc.code}：{detail}") from None
        except URLError:
            raise ValueError("无法连接本地服务，请先运行 job-radar serve") from None
