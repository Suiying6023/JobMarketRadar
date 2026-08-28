"""探针：opencode zen (https://opencode.ai/zen/v1) 哪些模型能匿名跑通 JD 提取。

结论见 memory/llm-endpoints.md。一次性探针，留档可复测。
关键发现：`key: public` 其实不是密钥——付费模型一律 401 Missing API key，
只有 `*-free` 档位接受匿名请求（走 IP 限额，返回 FreeUsageLimitError）。
"""
from __future__ import annotations

import json
import sys
import time
from urllib import error as urlerror
from urllib import request as urlreq

URL = "https://opencode.ai/zen/v1/chat/completions"

JD = """岗位职责：负责公司AI Agent平台的后端开发，基于Python/FastAPI构建大模型应用服务，
参与RAG知识库检索链路设计与优化，对接LangChain/LangGraph等框架。
任职要求：本科及以上学历，计算机相关专业，2026届应届生亦可；
熟悉Python，了解大模型应用开发（RAG/Prompt工程）；有向量数据库经验优先。
英语四级优先。薪资15-25K，双休，五险一金。"""

SYS = ("你是岗位画像提取器。从JD提取硬性要求，只输出JSON不要解释。"
       '格式：{"tech_req":[],"edu_req":[],"work_schedule":"双休/大小周/单休/未提及",'
       '"job_type":"开发岗/算法岗/产品岗/其他","grad_year":"26届可投/仅27届/仅实习/不限",'
       '"match":"keep/demote"}')

# 真实场景要求：能吃 2500 字 JD、并发 7、输出严格 JSON。
CANDIDATES = [
    "deepseek-v4-flash-free", "big-pickle", "mimo-v2.5-free",
    "ling-3.0-tiny-free", "ling-3.0-flash-free", "longcat-2.0-free",
    "laguna-s-2.1-free", "north-mini-code-free", "nemotron-3-ultra-free",
]


def probe(model: str, timeout: int = 120) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": f"岗位 JD：\n{JD}"}],
        "temperature": 0.1,
        "max_tokens": 4000,
    }).encode()
    # ⚠️ 必须带 User-Agent：不带的话 Cloudflare 回 403 error code 1010
    # （urllib 默认 UA 被拦，curl 不会——排查时两边表现不一致就是这个原因）
    req = urlreq.Request(URL, data=body, headers={
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0",
    })
    t = time.monotonic()
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urlerror.HTTPError as exc:
        return {"model": model, "ok": False, "code": exc.code,
                "err": exc.read().decode()[:160], "sec": round(time.monotonic() - t, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "ok": False, "code": -1,
                "err": f"{type(exc).__name__}: {exc}"[:160],
                "sec": round(time.monotonic() - t, 1)}
    el = round(time.monotonic() - t, 1)
    content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(content)
        valid = isinstance(parsed, dict) and "job_type" in parsed
    except json.JSONDecodeError:
        parsed, valid = None, False
    return {"model": model, "ok": True, "sec": el, "json_valid": valid,
            "usage": data.get("usage"), "parsed": parsed,
            "raw": content[:200] if not valid else None}


def main() -> None:
    models = sys.argv[1:] or CANDIDATES
    for m in models:
        r = probe(m)
        if not r["ok"]:
            print(f"[{r['code']}] {m:24s} {r['sec']:5.1f}s  {r['err']}")
        else:
            flag = "JSON-OK " if r["json_valid"] else "JSON-BAD"
            print(f"[200] {m:24s} {r['sec']:5.1f}s  {flag} usage={r['usage']}")
            if r["json_valid"]:
                print(f"      {json.dumps(r['parsed'], ensure_ascii=False)[:220]}")
            else:
                print(f"      raw: {r['raw']}")
        print()


if __name__ == "__main__":
    main()
