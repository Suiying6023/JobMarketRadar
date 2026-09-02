"""用 LLM 读 JD 全文做语义抽取，替代关键词统计。

**为什么要有这个脚本**（与 `capability_coverage.py` / `market_signals.py` 的区别）：

那两个脚本按关键词命中统计能力面。能力词在 JD 里是直接写出来的，所以
「有多少岗要求 RAG」用关键词是准的。但**「这个岗是不是真在建 agent」关键词判不了**：

- 上海万宝盛华《Agent开发》12-24k，JD 全文只有「C/C++/Python/Java/Go 都有岗！
  重点：代码能力强优先（可支持转行及无经验无相关背景人选）」——人力派遣。
- 博彦科技《Java开发工程师（agent）》，JD 全文两句：「负责Java相关系统的开发与维护」
  「参与agent模块的设计与实现」。

标题带 agent、内容与 agent 无关，任何关键词口径都会把它们算进「真 agent 岗」。
2026-09-02 手工精读 60 条实测：**真在建 agent 系统的只有 15%**。

所以本脚本让模型读全文，输出四分类与结构化场景描述。**字段顺序一律证据在结论之前**
（自回归生成顺序 = 承诺顺序，结论在前会让模型先下判断再补理由）。

筛选复用 `clean_local_db.should_demote`（含非外包：OD 按词边界、外包/外派/驻场/
劳务派遣、代招、匿名、薪资超纲），不另写一套阈值——两份会各自漂移。

用法：
    uv run python scripts/jd_semantic_survey.py --sample 60          # 抽样试跑
    uv run python scripts/jd_semantic_survey.py --sample 60 --seed 42
    uv run python scripts/jd_semantic_survey.py --all                # 全库
    uv run python scripts/jd_semantic_survey.py --report out.json    # 只看统计
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from clean_local_db import should_demote  # noqa: E402
from extract_glm_tags import USER_AGENT, load_env  # noqa: E402

DEFAULT_DB = ROOT / "data" / "hangzhou_agent.sqlite3"
DEFAULT_OUT = ROOT / "data" / "eval" / "jd_semantic_survey.json"

AGENT_REALITY = ("build_agent", "lowcode_config", "ai_assisted_dev", "unrelated")
REFERENCE_VALUE = ("high", "mid", "low")

SYSTEM_PROMPT = """你是招聘 JD 分析员。读完整条 JD，输出一个 JSON 对象。

只依据 JD 原文，不推测、不补全。JD 没写的一律填「未提及」或空数组。

字段（**必须按此顺序输出**）：

1. `scenario_desc`：这家公司要拿 AI/agent 做什么业务。写具体动作与对象，
   例如「审核差旅报销单据是否超标」「对接ERP自动处理跨境订单」。
   写不出具体业务就填「未提及」。禁止写「AI应用」「智能化升级」这类空话。

2. `product_form`：最终交付物形态。从这些里选一个或多个：
   `内部效率工具` `对客SaaS功能` `API服务` `数据流水线` `低代码工作流` `原型demo` `未提及`

3. `agent_stack`：JD 点名的技术，原样抄写，不归类。数组。
   例如 ["LangGraph", "Milvus", "FastAPI", "Docker"]

4. `capabilities`：JD 要求的能力面。只从这个封闭列表里选：
   `RAG检索` `知识库工程` `工作流编排` `多Agent协作` `工具调用` `提示工程`
   `记忆管理` `评测体系` `可观测轨迹` `人工审批` `引用溯源` `权限隔离`
   `长任务恢复` `幻觉治理` `结构化输出` `成本优化` `部署运维` `前端界面`

5. `engineering_hard_points`：JD 点名的工程难点，原样摘句。数组。
   例如 ["异常恢复", "上下文丢失", "幂等消费", "Prompt注入防护"]

6. `dispatch_evidence`：判断是否人力派遣/外包/驻场的依据，引 JD 原文。
   没有相关表述就写「无派遣相关表述」。

7. `is_dispatch`：`true` 或 `false`。依据上一条。
   判 true 的特征：多语言多岗位混招、「可支持转行」、只提代码能力不提业务、
   明写驻场/派遣/外包/人力资源服务。

8. `reality_evidence`：判断这个岗到底在做什么的依据，引 JD 原文关键句。

9. `agent_reality`：四选一。**结论必须与第 8 条依据一致**：
   如果依据里出现「以某某为主、叠加 AI 能力」「常规开发岗」这类描述，
   结论就不能是 `build_agent`。

   `build_agent`：**agent 本身就是要交付的产物**。判据是 JD 描述了 agent 的内部构造：
   编排/状态机、工具调用策略、RAG 链路各环节、记忆机制、评测体系、审批门之类。
   `lowcode_config`：主要在 Coze/Dify/扣子/n8n 等平台上配工作流。
   即便 JD 同时提到代码框架，只要「用低代码平台快速搭原型」是主要工作方式就选这个。
   `ai_assisted_dev`：**产物是常规系统，AI 只是其中一个被调用的能力或提效工具**。
   典型措辞：「接入 AI 能力」「封装大模型 API」「用 Cursor/Claude Code 提效」
   「以某语言后端为核心 + AI 工程化」。这类岗的主体工作是后端/全栈/算法部署。
   `unrelated`：JD 里没有任何 LLM 或 agent 相关工作内容
   （纯后端/CV/测试/RPA/硬件/售前/数据库等）。

   边界判据：**问「如果把 LLM 从这个岗位拿掉，剩下的工作还成立吗」**。
   还成立 → `ai_assisted_dev` 或 `unrelated`；不成立 → `build_agent`。

10. `reference_note`：这条 JD 对「设计一个 agent 作品集项目」的参考点是什么，
    或为什么没有参考价值。一到两句。

11. `reference_value`：`high` / `mid` / `low`，依据上一条。
    `high`：业务场景具体 + 技术栈完整 + 有工程难点，可直接照着设计项目
    `mid`：有部分可用信息
    `low`：无参考价值

只输出 JSON，不要解释，不要代码围栏。"""

REQUIRED_FIELDS = (
    "scenario_desc", "product_form", "agent_stack", "capabilities",
    "engineering_hard_points", "dispatch_evidence", "is_dispatch",
    "reality_evidence", "agent_reality", "reference_note", "reference_value",
)


def validate(rec: dict) -> str | None:
    """返回错误说明，None 表示通过。校验不过就重试——宁可这条不入库，
    也不要存一条判定依据缺失的记录。"""
    missing = [f for f in REQUIRED_FIELDS if f not in rec]
    if missing:
        return f"缺字段: {','.join(missing)}"
    if rec["agent_reality"] not in AGENT_REALITY:
        return f"agent_reality 非法: {rec['agent_reality']!r}"
    if rec["reference_value"] not in REFERENCE_VALUE:
        return f"reference_value 非法: {rec['reference_value']!r}"
    if not isinstance(rec["is_dispatch"], bool):
        return f"is_dispatch 非布尔: {rec['is_dispatch']!r}"
    # 两个理由字段不许空：空理由是漏判的特征信号（与 extract_glm_tags 同一判据）
    for f in ("reality_evidence", "reference_note"):
        if not str(rec.get(f) or "").strip():
            return f"{f} 为空"
    for f in ("agent_stack", "capabilities", "engineering_hard_points"):
        if not isinstance(rec[f], list):
            return f"{f} 非数组"
    return None


def call_llm(env: dict, row: dict, max_retries: int = 4) -> tuple[dict | None, str | None, int]:
    """调用 LLM。返回 (记录, 错误, 尝试次数)。

    UA 必须伪装：网关在 Cloudflare 后面，默认 `Python-urllib` 被拦成
    `403 upstream_error`，看着像上游模型挂了但重试永远救不回来。
    """
    import urllib.error as urlerror
    import urllib.request as urlreq

    head = f"岗位标题：{row['title']}\n公司：{row['company_name']}\n"
    head += f"薪资：{row['salary_min_k']}-{row['salary_max_k']}k\n"
    head += f"公司规模：{row['company_size']}　融资：{row['financing_stage']}\n"
    body = json.dumps({
        "model": env["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{head}岗位 JD：\n{row['job_description'][:3000]}"},
        ],
        "temperature": 0.1,
        "max_tokens": 3000,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        req = urlreq.Request(env["LLM_API_URL"], data=body, headers={
            "Authorization": f"Bearer {env['LLM_API_KEY']}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urlreq.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
            content = ((json.loads(raw).get("choices") or [{}])[0]
                       .get("message", {}).get("content") or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("非 JSON 对象")
            bad = validate(parsed)
            if bad:
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                return None, bad, attempt
            return parsed, None, attempt
        except urlerror.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                time.sleep(attempt * 4)
                continue
            if attempt >= max_retries:
                return None, f"HTTP {exc.code}", attempt
            time.sleep(2)
        except Exception as exc:  # noqa: BLE001 — 外部边界，逐次重试后放弃
            if attempt >= max_retries:
                return None, f"{type(exc).__name__}: {exc}"[:120], attempt
            time.sleep(2)
    return None, "重试用尽", attempt


def load_pool(db_path: Path, lo: float, hi: float) -> tuple[list[dict], Counter]:
    """取候选池。筛选复用 should_demote，它已覆盖非外包与薪资超纲。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE glm_tags_json NOT IN ('', '{}')
          AND json_extract(glm_tags_json, '$.match') = 'keep'
          AND job_description != ''
          AND salary_max_k BETWEEN ? AND ?
    """, (lo, hi)).fetchall()
    conn.close()

    pool, dropped = [], Counter()
    for r in rows:
        demote, reason = should_demote(r)
        if demote:
            dropped[reason.split("[")[0]] += 1
            continue
        pool.append(dict(r))
    return pool, dropped


def run(env: dict, rows: list[dict], concurrency: int) -> tuple[list[dict], list[dict]]:
    done, failed = [], []
    t0 = time.time()

    def work(row: dict) -> tuple[dict, dict | None, str | None, int]:
        rec, err, tries = call_llm(env, row)
        return row, rec, err, tries

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(work, r): r for r in rows}
        for i, fut in enumerate(as_completed(futures), 1):
            row, rec, err, tries = fut.result()
            if rec is None:
                failed.append({"canonical_id": row["canonical_id"],
                               "title": row["title"], "error": err})
                mark = f"失败({err})"
            else:
                rec["canonical_id"] = row["canonical_id"]
                rec["title"] = row["title"]
                rec["company_name"] = row["company_name"]
                rec["salary_max_k"] = row["salary_max_k"]
                rec["company_size"] = row["company_size"]
                rec["financing_stage"] = row["financing_stage"]
                done.append(rec)
                mark = f"{rec['agent_reality']:<16} ref={rec['reference_value']}"
            print(f"[{i}/{len(rows)}] {mark}  {row['title'][:28]}", flush=True)

    print(f"\n完成 {len(done)} 失败 {len(failed)} 用时 {time.time()-t0:.0f}s")
    return done, failed


def report(recs: list[dict]) -> None:
    n = len(recs)
    if not n:
        print("无记录")
        return
    print(f"\n{'='*60}\n样本 {n} 条\n{'='*60}")

    print("\n== 岗位性质（关键词法判不出的那一层）")
    for k, c in Counter(r["agent_reality"] for r in recs).most_common():
        print(f"  {k:<18} {c:>4}  {c/n:>4.0%}")

    disp = sum(1 for r in recs if r["is_dispatch"])
    print(f"\n== 模型二次识别出的派遣/外包：{disp} 条（{disp/n:.0%}）")
    print("   注：候选池已过 should_demote 的外包名单，这里是名单之外漏网的")

    print("\n== 参考价值")
    for k, c in Counter(r["reference_value"] for r in recs).most_common():
        print(f"  {k:<6} {c:>4}  {c/n:>4.0%}")

    build = [r for r in recs if r["agent_reality"] == "build_agent"]
    print(f"\n== 真建 agent 那批（{len(build)} 条）的能力面")
    cap = Counter(c for r in build for c in r["capabilities"])
    for k, c in cap.most_common(20):
        print(f"  {k:<14} {c:>4}  {c/max(len(build),1):>4.0%}")

    print(f"\n== 真建 agent 那批的技术栈（原样抄写，前 25）")
    for k, c in Counter(s for r in build for s in r["agent_stack"]).most_common(25):
        print(f"  {k:<22} {c:>4}")

    print(f"\n== 产品形态")
    for k, c in Counter(f for r in recs for f in
                        (r["product_form"] if isinstance(r["product_form"], list)
                         else [r["product_form"]])).most_common():
        print(f"  {k:<16} {c:>4}")

    print(f"\n== 工程难点（真建 agent 那批，前 20）")
    for k, c in Counter(h for r in build for h in r["engineering_hard_points"]).most_common(20):
        print(f"  {k:<20} {c:>4}")

    print(f"\n== high 参考价值的业务场景（这是设计项目的原料）")
    for r in recs:
        if r["reference_value"] == "high":
            print(f"\n  [{r['salary_max_k']:.0f}k {r['company_size']} {r['financing_stage']}] "
                  f"{r['company_name']} | {r['title'][:30]}")
            print(f"    场景：{r['scenario_desc']}")
            print(f"    形态：{r['product_form']}")
            print(f"    参考：{r['reference_note']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--band", nargs=2, type=float, default=[12, 28], metavar=("LO", "HI"))
    ap.add_argument("--sample", type=int, help="随机抽样条数")
    ap.add_argument("--all", action="store_true", help="跑全池")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="并发数。中转有速率上限，默认保守")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--report", type=Path, help="只对已有结果出统计，不调模型")
    args = ap.parse_args()

    if args.report:
        report(json.loads(args.report.read_text(encoding="utf-8"))["records"])
        return 0

    if not args.sample and not args.all:
        ap.error("要么给 --sample N，要么给 --all")

    pool, dropped = load_pool(args.db, args.band[0], args.band[1])
    print(f"候选池 {len(pool)} 条（薪资上限 {args.band[0]:.0f}-{args.band[1]:.0f}k "
          f"∩ match=keep ∩ 有JD ∩ 过 should_demote）")
    if dropped:
        print("被 should_demote 挡掉：" +
              "　".join(f"{k} {v}" for k, v in dropped.most_common(8)))
    if not pool:
        print("候选池为空")
        return 1

    if args.all:
        rows = pool
    else:
        random.seed(args.seed)
        rows = random.sample(pool, min(args.sample, len(pool)))
    print(f"本轮处理 {len(rows)} 条，并发 {args.concurrency}\n")

    env = load_env()
    done, failed = run(env, rows, args.concurrency)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": env["LLM_MODEL"],
            "band": args.band,
            "pool_size": len(pool),
            "sampled": len(rows),
            "seed": args.seed if args.sample else None,
            "succeeded": len(done),
            "failed": len(failed),
        },
        "records": done,
        "failures": failed,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写出 {args.out}")

    report(done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
