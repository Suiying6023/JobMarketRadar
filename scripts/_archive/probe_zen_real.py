"""探针：用**真实 SYSTEM_PROMPT + 真实 JD** 压测 opencode zen 免费档能否替代 stepfun。

判据（对齐 extract_glm_tags.py 的实际用法）：
1. 吃得下完整 SYSTEM_PROMPT（约 1800 字）+ JD 2500 字
2. 严格输出 JSON，且字段齐全（match/grad_year/work_schedule/job_type 都要有）
3. 并发 7 下不被限流打死
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parent.parent.parent
URL = "https://opencode.ai/zen/v1/chat/completions"

# 复用生产脚本里的真实 prompt，避免"探针用简化 prompt 结论不成立"
spec = importlib.util.spec_from_file_location(
    "extract_glm_tags", ROOT / "scripts" / "extract_glm_tags.py")
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
SYSTEM_PROMPT = _mod.SYSTEM_PROMPT

REQUIRED = ("exp_req", "edu_req", "tech_req", "job_type",
            "grad_year", "work_schedule", "match")


def call(model: str, jd: str, timeout: int = 180) -> tuple[dict | None, str | None, float]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": f"岗位 JD：\n{jd[:2500]}"}],
        "temperature": 0.1,
        "max_tokens": 4000,
    }).encode()
    req = urlreq.Request(URL, data=body, headers={
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0",   # 不带会被 Cloudflare 403 (code 1010)
    })
    t = time.monotonic()
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error", {}).get("type", "")
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {exc.code} {detail}", time.monotonic() - t
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}", time.monotonic() - t
    el = time.monotonic() - t
    content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, f"BAD_JSON: {content[:80]}", el
    if not isinstance(parsed, dict):
        return None, "NOT_OBJECT", el
    missing = [k for k in REQUIRED if k not in parsed]
    if missing:
        return None, f"MISSING {missing}", el
    return parsed, None, el


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=7)
    args = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "hangzhou_agent.sqlite3")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_id, title, job_description FROM jobs "
        "WHERE retention_status='active' AND job_description != '' "
        "ORDER BY length(job_description) DESC LIMIT ?", (args.n,)).fetchall()
    con.close()
    print(f"模型 {args.model}｜{len(rows)} 条（取最长 JD，最严场景）｜并发 {args.concurrency}")

    ok = bad = 0
    errs: dict[str, int] = {}
    lat: list[float] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(call, args.model, r["job_description"]): r for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            parsed, err, el = fut.result()
            lat.append(el)
            if parsed:
                ok += 1
                print(f"  ✓ {el:5.1f}s {r['title'][:22]:24s} "
                      f"match={parsed['match']} type={parsed['job_type']} "
                      f"grad={parsed['grad_year']}")
            else:
                bad += 1
                errs[err] = errs.get(err, 0) + 1
                print(f"  ✗ {el:5.1f}s {r['title'][:22]:24s} {err}")
    el_all = time.monotonic() - t0
    print(f"\n成功 {ok}/{len(rows)}，失败 {bad}｜总耗时 {el_all:.0f}s"
          f"｜平均单条 {sum(lat)/len(lat):.1f}s｜吞吐 {ok/el_all*60:.1f} 条/分")
    if errs:
        print("失败分布:", errs)
    if ok:
        print(f"→ 推算 504 条需要约 {504/max(ok/el_all*60, 0.01):.0f} 分钟")


if __name__ == "__main__":
    main()
