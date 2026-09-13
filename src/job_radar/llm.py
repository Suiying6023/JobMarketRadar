"""Small bounded Chat Completions client; no provider credentials enter logs."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.parse import urlsplit


class LLMError(RuntimeError):
    pass


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        old, new = urlsplit(request.full_url), urlsplit(new_url)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise LLMError("模型端点跨域重定向，请直接配置目标地址")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def extract_tags(config, record: dict) -> tuple[dict, dict]:
    endpoint = config.base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    key = config.api_key or (os.environ.get(config.api_key_env, "") if config.api_key_env else "")
    payload = {"model": config.model, "messages": [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": json.dumps(record, ensure_ascii=False)}],
        "temperature": 0, "max_tokens": config.max_tokens, "stream": False}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = Request(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
    try:
        # Explicit endpoint routing; local model traffic does not inherit system proxy settings.
        with build_opener(ProxyHandler({}), SameOriginRedirect()).open(request, timeout=config.timeout_seconds) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise LLMError("模型响应超过 2 MB")
        response = json.loads(raw)
        content = response["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or len(content) > 200000:
            raise LLMError("模型没有返回可读取的 JSON 文本")
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            raise LLMError("标签输出需要 JSON 对象")
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        json.dumps([result, usage], allow_nan=False)
        return result, usage
    except HTTPError as exc:
        raise LLMError(f"模型端点返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise LLMError("模型端点连接失败或超时") from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise LLMError("模型响应格式无效，请检查模型与提示词") from None
