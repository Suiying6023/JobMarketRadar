"""Small bounded Chat Completions client; no provider credentials enter logs.

## ⚠️ `extract_tags` 产出的是**旧标签**，已被 `src/job_analysis` 取代

2026-10-18 抽查：旧标签在「是否接受应届」这一项上大量凭空生成——
2,886/3,813 条「接受应届」与 2,814/2,919 条「不接受应届」的 JD 正文里**根本没提应届**。
根因是让 chat 模型自由输出 JSON，没有可核对的判据。

现在的标签走 `src/job_analysis`（决策模型 + TypeSafe 协议 + 固定选项集），
7,417 条已重标，与平台 `jobType` 交叉验证一致率 91.2%–98.2%。

这个函数保留是因为 `job-radar tag` 任务与 `glm_tags_json` 列还在，
但**不要再拿它的输出去做判断**。要重标跑 `python -m job_analysis.relabel`。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import unicodedata
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.parse import urlsplit


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        old, new = urlsplit(request.full_url), urlsplit(new_url)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise LLMError("模型端点跨域重定向，请直接配置目标地址")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def extract_tags(config, record: dict) -> tuple[dict, dict]:
    for attempt in range(2):
        try:
            result, usage = _extract_once(config, record)
            usage = dict(usage)
            usage["client_transport_attempts"] = attempt + 1
            return result, usage
        except LLMError as exc:
            if not exc.retryable or attempt == 1:
                raise
            time.sleep(2)


def _extract_once(config, record: dict) -> tuple[dict, dict]:
    endpoint = config.base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    key = config.api_key or (os.environ.get(config.api_key_env, "") if config.api_key_env else "")
    payload = {"model": config.model, "messages": [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": json.dumps(record, ensure_ascii=False)}],
        "temperature": 0, "max_tokens": config.max_tokens, "stream": False}
    effort = config.reasoning_effort
    if effort == "auto" and config.model == "stealth/space-bunny-alpha":
        effort = "low"
    if effort != "auto":
        payload["reasoning_effort"] = effort
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
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("模型输出达到 token 上限，请增加 max_tokens 或缩短输入")
        if choice["message"].get("refusal"):
            raise LLMError("模型拒绝该次抽取")
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or len(content) > 200000:
            raise LLMError("模型没有返回可读取的 JSON 文本")
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(content)
        if not isinstance(result, dict) or not result:
            raise LLMError("标签输出需要非空 JSON 对象")
        from .service_config import DEFAULT_TAG_PROMPT
        if config.system_prompt == DEFAULT_TAG_PROMPT:
            spans = {span["id"]: span["text"] for span in record.get("source_spans", [])}
            for ids_name, text_name in (("evidence_ids", "evidence"), ("requirement_note_ids", "requirement_notes")):
                selected = result.get(ids_name)
                if not isinstance(selected, list):
                    raise LLMError(f"原文片段ID需要数组：{ids_name}")
                invalid = [item for item in selected if not isinstance(item, str) or item not in spans]
                if invalid:
                    raise LLMError(f"抽取引用了不存在的原文片段ID：{ids_name}，无效项数{len(invalid)}")
                # Bound valid references without discarding an otherwise grounded result.
                result[ids_name] = list(dict.fromkeys(selected))[:8]
                result[text_name] = [spans[item] for item in result[ids_name]]
            graduate_labels = {0: "未说明", 1: "明确接受", 2: "优秀应届可放宽", 3: "明确不接受"}
            graduate_code = result.get("fresh_graduate")
            if type(graduate_code) is not int or graduate_code not in record.get("allowed_fresh_graduate_codes", [0]):
                raise LLMError("应届表态编码无效或缺少原文依据")
            result["fresh_graduate"] = graduate_labels[graduate_code]
            for name in ("role", "education", "experience", "employment_type", "fresh_graduate", "school_requirement", "work_schedule"):
                if not isinstance(result.get(name), str):
                    raise LLMError(f"抽取缺少文本字段：{name}")
            for name in ("skills", "responsibilities", "evidence", "required_skills", "preferred_skills", "graduation_years", "requirement_notes"):
                if not isinstance(result.get(name), list) or any(not isinstance(value, str) for value in result[name]):
                    raise LLMError(f"抽取数组格式无效：{name}")
            def normalized(value):
                return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()
            sources = [normalized(str(record.get(name) or "")) for name in
                ("title", "company", "city", "education", "experience", "job_description")]
            if any(not quote.strip() or not any(normalized(quote) in source for source in sources)
                   for quote in result["evidence"] + result["requirement_notes"]):
                raise LLMError("抽取引文无法在输入原文中定位，不保存结果")
            graduate_source = normalized(str(record.get("title") or "") + "\n" + str(record.get("job_description") or ""))
            if result["fresh_graduate"] == "明确不接受" and not re.search(
                r"(?:不接受|不考虑|不招收?|不接收|不要|拒绝).{0,8}应届|应届.{0,8}(?:不接受|不考虑|勿投|不招|请勿)", graduate_source):
                raise LLMError("原文没有明确拒绝应届的表述，不保存推断为明确不接受的结果")
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        json.dumps([result, usage], allow_nan=False)
        return result, usage
    except HTTPError as exc:
        raise LLMError(f"模型端点返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) and isinstance(exc.reason, BaseException) else exc
        retryable = isinstance(reason, (TimeoutError, ConnectionError, RemoteDisconnected, ssl.SSLEOFError))
        retryable = retryable or getattr(reason, "errno", None) in (10054, 10060, 104, 110, 54)
        raise LLMError(f"模型端点连接失败或超时（{type(reason).__name__}）", retryable=retryable) from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise LLMError("模型响应格式无效，请检查模型与提示词") from None
