"""测试智谱 GLM-4.7-Flash（免费多并发）对岗位 JD 标签提取的可用程度。

目的：
1. 测并发能力与延迟（多并发请求的成功率、吞吐）
2. 测提取质量：能否从 JD 正文正确抓出经验/学历/院校/技术栈/其他硬性门槛，
   区分硬性 vs 加分，并判断岗位类型（开发/设计/运营/市场）——用于识别跑题岗。

用法:
  python scripts/test_glm_labels.py --limit 10 --concurrency 5
API key 从 .env 的 ZHIPU_API_KEY 读取，不落代码。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

API_URL = os.environ.get("GLM_API_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
MODEL = os.environ.get("GLM_MODEL", "GLM-4.7-Flash")

SYSTEM_PROMPT = """你是岗位画像提取器。从岗位 JD 正文中提取硬性要求与加分项，只输出 JSON，不要任何解释。

输出格式：
{
  "exp_req": ["经验不限", "经验≥2年"],
  "edu_req": ["本科及以上", "硕士", "博士"],
  "school_req": ["985/211", "双一流", "全日制"],
  "tech_req": ["Python", "Java", "大模型", "RAG", "Agent开发"],
  "other_req": ["英语六级", "需出差", "驻场", "证书"],
  "bonus": ["硕士优先", "有RAG经验加分"],
  "job_type": "开发岗/算法岗/测试岗/设计岗/运营岗/市场岗/产品岗/内容岗/其他"
}

要求：
- 所有标签必须来自 JD 正文原文依据，不要编造。
- 区分硬性要求（必须满足）与加分项（写明"优先/加分/更佳"的）。
- edu_req 以 JD 正文表述为准（如"硕士及以上"），不要依赖岗位标题。
- job_type 判断这个岗位实际做什么（开发/算法/测试/设计/运营/市场/产品/内容），AIGC/设计类岗位请标注为设计岗或内容岗。"""


def load_key() -> str:
    env = ROOT / ".env"
    if os.environ.get("ZHIPU_API_KEY"):
        return os.environ["ZHIPU_API_KEY"]
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ZHIPU_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 ZHIPU_API_KEY（环境变量或 .env）")


def call_glm(key: str, jd: str, retries: int = 3) -> tuple[float, dict | None, str | None]:
    """单次调用，支持 429 退避重试。返回 (耗时秒, 解析出的 JSON dict 或 None, 错误信息或 None)。"""
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"岗位 JD：\n{jd[:2000]}"},
        ],
        "temperature": 0.1,
        "max_tokens": 5000,  # step-3.7-flash 强制推理，reasoning 占大头，给足额度
        "enable_thinking": False,  # 3.7 无法真正关闭，但对有该参数的路由有助
    }).encode("utf-8")
    req = urlreq.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urlreq.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
            elapsed = time.monotonic() - start
            data = json.loads(raw)
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            return elapsed, parsed, None
        except urlerror.HTTPError as exc:
            if exc.code == 429 and attempt <= retries:
                time.sleep(attempt * 3)  # 退避：3s, 6s, 9s
                continue
            return time.monotonic() - start, None, f"HTTP {exc.code}"
        except (urlerror.URLError, json.JSONDecodeError, TimeoutError) as exc:
            return time.monotonic() - start, None, str(exc)[:120]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="测试岗位条数")
    parser.add_argument("--concurrency", type=int, default=5, help="并发请求数")
    parser.add_argument("--interval", type=float, default=0, help="串行模式：每条间隔秒数（>0 时忽略并发）")
    parser.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    args = parser.parse_args()

    key = load_key()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, company_name, jd_tags_json, job_description FROM jobs "
        "WHERE retention_status = 'active' AND job_description != '' "
        "ORDER BY RANDOM() LIMIT ?", (args.limit,)
    ).fetchall()
    print(f"测试 {len(rows)} 条岗位，模式 {'串行间隔%.0fs' % args.interval if args.interval else '并发%d' % args.concurrency}，模型 {MODEL}\n")

    results: list[dict] = []
    latencies: list[float] = []
    if args.interval:
        for r in rows:
            elapsed, parsed, err = call_glm(key, r["job_description"])
            latencies.append(elapsed)
            results.append({"row": r, "elapsed": elapsed, "parsed": parsed, "err": err})
            time.sleep(args.interval)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(call_glm, key, r["job_description"]): r for r in rows
            }
            for fut in as_completed(futures):
                r = futures[fut]
                elapsed, parsed, err = fut.result()
                latencies.append(elapsed)
                results.append({"row": r, "elapsed": elapsed, "parsed": parsed, "err": err})
    results.sort(key=lambda x: x["elapsed"])

    ok = sum(1 for x in results if x["parsed"])
    print(f"=== 并发统计 ===")
    print(f"成功 {ok}/{len(results)} | 平均延迟 {sum(latencies)/max(len(latencies),1):.2f}s | "
          f"最快 {min(latencies):.2f}s | 最慢 {max(latencies):.2f}s | 总耗时(串行) {sum(latencies):.1f}s")
    print()

    for i, x in enumerate(results, 1):
        r, p, err = x["row"], x["parsed"], x["err"]
        print(f"--- {i}. {r['title'][:24]} | {r['company_name'][:14]} | {x['elapsed']:.1f}s ---")
        if err:
            print(f"  [失败] {err}")
            continue
        if not p:
            print("  [解析失败]")
            continue
        print(f"  job_type: {p.get('job_type','?')}")
        print(f"  exp: {p.get('exp_req')} | edu: {p.get('edu_req')} | school: {p.get('school_req')}")
        print(f"  tech: {p.get('tech_req')} | other: {p.get('other_req')} | bonus: {p.get('bonus')}")

    # 保存结果
    out = ROOT / "output" / "glm_test_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([
        {
            "title": x["row"]["title"], "company": x["row"]["company_name"],
            "elapsed_s": round(x["elapsed"], 2), "error": x["err"],
            "model_tags": x["parsed"], "rule_tags": json.loads(x["row"]["jd_tags_json"] or "{}"),
        } for x in results
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存: {out}")


if __name__ == "__main__":
    main()
