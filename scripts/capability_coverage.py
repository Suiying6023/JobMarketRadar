"""按「能力面」而非「业务场景」统计 JD 覆盖率，回答项目底座是否通用。

**为什么要有这个脚本**（与 `market_signals.py --section scene` 的区别）：

`market_signals.py` 按场景分类，回答"公司要拿 agent 干什么"。它的问题是
**场景标签之间不可加** —— "知识库问答 194 条"与"文档/合规质控 58 条"重叠严重，
把两个数字相加没有意义，也无法回答"我这个项目能投多少岗"。

本脚本换一个切法：**把项目已实现的能力逐条对到 JD 要求上**，统计
① 每个能力被多少 JD 点名 ② 一条 JD 平均命中几个能力
③ **至少命中 N 个能力的 JD 有多少** —— 第三个数字才是"项目覆盖面"。

判据：一个岗位要求 RAG + 评测 + 权限，另一个要求 RAG + 工具调用 + 可观测，
两者的**业务场景可以完全不同**（一个做知识库、一个做客服），
但**底座是同一套**。场景统计看不出这一点，能力统计能。

用法：
    uv run python scripts/capability_coverage.py                  # 默认可达带
    uv run python scripts/capability_coverage.py --band 20 35
    uv run python scripts/capability_coverage.py --min-caps 4     # 换"覆盖"的门槛
    uv run python scripts/capability_coverage.py --dump 合规 -n 5 # 看命中 JD 原文
    uv run python scripts/capability_coverage.py --section combo  # 只看能力组合

三层筛选与 `market_signals.py` 保持一致（match=keep ∩ strong 词表 ∩ 薪资带），
改动其中任何一层，两个脚本要同步改，否则两份结论无法互相印证。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

# 与 market_signals.py 同一份词表。判定"真 agent/LLM 岗"。
STRONG = re.compile(
    r"Agent|智能体|LLM|大模型|大语言模型|RAG|MCP|LangChain|LangGraph|Dify|Coze"
    r"|工作流编排|工具调用|AIGC|AI\s*原生", re.I)

# 能力面 = ComplianceAudit 项目实际实现或计划实现的东西，逐条对到 JD 措辞。
# 命名与 openspec/specs/ 下的能力域对齐，便于回答"这条需求对应市场上哪种要求"。
CAPABILITIES: dict[str, str] = {
    "检索增强(RAG)": r"RAG|检索增强|向量检索|向量数据库|embedding|语义检索|召回",
    "混合检索/重排": r"混合检索|BM25|rerank|重排|多路召回|倒排",
    "知识库工程": r"知识库|知识图谱|文档解析|切片|chunk|语料|数据清洗",
    "引用溯源": r"溯源|引用|出处|可解释|归因|证据|引证",
    "工作流编排": r"工作流|编排|LangGraph|状态机|DAG|流程引擎|pipeline",
    "工具调用": r"Function\s*Call|工具调用|tool\s*call|插件|MCP|API\s*调用",
    "结构化输出": r"结构化输出|JSON\s*Schema|格式化输出|schema|结构化抽取",
    "多Agent协作": r"多\s*[Aa]gent|Multi-?Agent|多智能体|协作|子\s*agent",
    "评测体系": r"评测|评估体系|benchmark|指标体系|badcase|A/?B\s*测试|效果验证",
    "可观测/轨迹": r"可观测|observability|trace|链路|轨迹|监控|日志分析|埋点",
    "人工审批/HITL": r"人工审核|人工介入|human.?in.?the.?loop|审批|人工复核|人工确认",
    "权限/多租户": r"权限|租户|tenant|ACL|数据隔离|访问控制|鉴权|脱敏",
    "幻觉治理": r"幻觉|hallucination|事实性|准确性保障|一致性校验|兜底",
    "提示工程": r"[Pp]rompt|提示词|上下文工程|context\s*engineering",
    "长任务/异常恢复": r"长任务|长链路|异常恢复|重试|失败恢复|断点|容错|熔断",
    "合规/审核判定": r"合规|风控|审核|稽核|质检|规则引擎|政策|法规|制度",
    "后端工程": r"FastAPI|Flask|Django|微服务|高并发|分布式|Redis|PostgreSQL|MySQL",
    "部署运维": r"Docker|K8s|Kubernetes|容器|CI/?CD|部署|运维|云原生",
}


def load(band: tuple[int, int], *, match_filter: bool = True) -> list[dict]:
    """取样本。三层筛选缺一层结论就会错，见模块 docstring。"""
    if not DB.exists():
        sys.exit(f"找不到数据库：{DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = []
    lo, hi = band
    sql = """
        SELECT title, company_name, company_size, financing_stage, salary_max_k,
               job_description, glm_tags_json
        FROM jobs
        WHERE job_description IS NOT NULL AND job_description != ''
          AND salary_max_k IS NOT NULL AND salary_max_k >= ? AND salary_max_k <= ?
    """
    for row in conn.execute(sql, (lo, hi)):
        jd = row["job_description"] or ""
        if not STRONG.search(jd) and not STRONG.search(row["title"] or ""):
            continue
        if match_filter:
            tags = row["glm_tags_json"]
            if not tags:
                continue
            try:
                if json.loads(tags).get("match") != "keep":
                    continue
            except (json.JSONDecodeError, AttributeError):
                continue
        rows.append(dict(row))
    conn.close()
    return rows


def hits_of(jd: str, title: str) -> set[str]:
    text = f"{title}\n{jd}"
    return {name for name, pat in CAPABILITIES.items() if re.search(pat, text, re.I)}


def show_caps(rows: list[dict]) -> None:
    """每个能力被多少 JD 点名。单项出现率决定"能不能进面"。"""
    counter: collections.Counter[str] = collections.Counter()
    sal: dict[str, list[float]] = collections.defaultdict(list)
    for r in rows:
        for name in hits_of(r["job_description"], r["title"] or ""):
            counter[name] += 1
            sal[name].append(r["salary_max_k"])
    total = len(rows)
    print(f"\n== 能力面出现率（样本 {total} 条）")
    print(f"{'能力':<16}{'条数':>6}{'占比':>8}{'薪资中位':>10}")
    for name, n in counter.most_common():
        med = statistics.median(sal[name]) if sal[name] else 0
        print(f"{name:<16}{n:>6}{n / total:>7.0%}{med:>9.0f}k")


def show_coverage(rows: list[dict], min_caps: int) -> None:
    """⭐ 核心数字：一条 JD 命中几个能力，以及至少命中 N 个的占多少。"""
    dist: collections.Counter[int] = collections.Counter()
    for r in rows:
        dist[len(hits_of(r["job_description"], r["title"] or ""))] += 1
    total = len(rows)
    print(f"\n== 单条 JD 命中能力数分布（共 {len(CAPABILITIES)} 个能力）")
    cum = 0
    for k in sorted(dist, reverse=True):
        cum += dist[k]
        print(f"  命中 {k:>2} 个：{dist[k]:>4} 条   累计 ≥{k}：{cum:>4} 条 ({cum / total:.0%})")
    covered = sum(n for k, n in dist.items() if k >= min_caps)
    counts = [k for k, n in dist.items() for _ in range(n)]
    print(f"\n  平均命中 {statistics.mean(counts):.1f} 个，中位 {statistics.median(counts):.0f} 个")
    print(f"  **≥{min_caps} 个能力的 JD：{covered} 条 / {total} 条 = {covered / total:.0%}**")


def show_combo(rows: list[dict], top: int = 15) -> None:
    """能力组合。单项说明词频，组合说明一个岗位的完整画像。"""
    combos: collections.Counter[tuple[str, ...]] = collections.Counter()
    for r in rows:
        hits = hits_of(r["job_description"], r["title"] or "")
        if len(hits) >= 3:
            combos[tuple(sorted(hits))] += 1
    print(f"\n== 高频能力组合（≥3 项，前 {top}）")
    for combo, n in combos.most_common(top):
        print(f"  {n:>3} 条  {' + '.join(combo)}")


def show_core_overlap(rows: list[dict]) -> None:
    """本项目主打的五个能力，两两共现。回答"底座是否通用"。"""
    core = ["检索增强(RAG)", "引用溯源", "权限/多租户", "人工审批/HITL", "评测体系",
            "工作流编排", "合规/审核判定"]
    print("\n== 本项目能力面的两两共现（行=A，列=B，值=同时要求 A 与 B 的 JD 数）")
    sets = {c: set() for c in core}
    for i, r in enumerate(rows):
        for name in hits_of(r["job_description"], r["title"] or "") & set(core):
            sets[name].add(i)
    head = "".join(f"{c[:6]:>9}" for c in core)
    print(f"{'':<16}{head}")
    for a in core:
        cells = "".join(f"{len(sets[a] & sets[b]):>9}" for b in core)
        print(f"{a:<16}{cells}")


def dump(rows: list[dict], needle: str, limit: int) -> None:
    pat = re.compile(needle, re.I)
    shown = 0
    for r in rows:
        if not pat.search(r["job_description"]):
            continue
        print(f"\n{'=' * 70}\n{r['title']} | {r['company_name']} | "
              f"{r['salary_max_k']}k | {r['company_size']} | {r['financing_stage']}")
        caps = sorted(hits_of(r["job_description"], r["title"] or ""))
        print(f"命中能力（{len(caps)}）：{', '.join(caps)}")
        print("-" * 70)
        print(r["job_description"][:1500])
        shown += 1
        if shown >= limit:
            break
    if not shown:
        print(f"没有 JD 命中 {needle!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", nargs=2, type=int, default=[12, 28], metavar=("LO", "HI"))
    ap.add_argument("--min-caps", type=int, default=5,
                    help="命中几个能力算'本项目覆盖'（默认 5）")
    ap.add_argument("--no-match-filter", action="store_true")
    ap.add_argument("--section", choices=["all", "caps", "coverage", "combo", "overlap"],
                    default="all")
    ap.add_argument("--dump", metavar="KEYWORD")
    ap.add_argument("-n", type=int, default=5)
    args = ap.parse_args()

    rows = load((args.band[0], args.band[1]), match_filter=not args.no_match_filter)
    if not rows:
        sys.exit("样本为空，放宽 --band 或加 --no-match-filter")

    med = statistics.median([r["salary_max_k"] for r in rows])
    print(f"样本 {len(rows)} 条（薪资上限 {args.band[0]}-{args.band[1]}k "
          f"∩ match=keep ∩ 真 agent 岗），薪资上限中位 {med:.0f}k")

    if args.dump:
        dump(rows, args.dump, args.n)
        return
    if args.section in ("all", "caps"):
        show_caps(rows)
    if args.section in ("all", "coverage"):
        show_coverage(rows, args.min_caps)
    if args.section in ("all", "combo"):
        show_combo(rows)
    if args.section in ("all", "overlap"):
        show_core_overlap(rows)


if __name__ == "__main__":
    main()
