"""从 JD 库反推需求侧信号：场景分布、能力要求出现率、薪资归因。

**设计意图**：回答"面试项目该做什么"，而不是"哪些岗位薪资高"。
这两个问题的答案不同 —— 见下面「薪资归因」那节的判据。

用法：
    uv run python scripts/market_signals.py                       # 默认可达带
    uv run python scripts/market_signals.py --band 20 35          # 换薪资带
    uv run python scripts/market_signals.py --no-match-filter     # 不按 LLM 标签筛
    uv run python scripts/market_signals.py --section attrib      # 只看薪资归因
    uv run python scripts/market_signals.py --dump-jd 可观测 -n 8  # 打印命中 JD 原文

## 三层筛选，缺一层结论就会错（都是踩出来的）

1. **`glm_tags.match='keep'`**：LLM 已按应届/学历/排除项判过可达性。
   不筛这层的话统计里混着"3 年以上""硕士起"，得出的能力清单投不上。
2. **strong 词表筛真 agent 岗**：宽口径场景词（"搜索""设备""运维"这类通用名词）
   噪音极大。同一个"搜索"既是推荐系统也是 Ctrl-F。
3. **⚠️ 薪资带**：`keep` 只判硬门槛，判不出"这岗实际要 3 年工程经验"。
   60-90k 的 Agent Infra 写着「接受应届」，但要求「能清晰描述一次文件写入
   从调用到落盘的全过程」—— 硬门槛过了，实际门槛没过。**必须再叠一道薪资带。**

## 薪资归因：能力词决定能不能过筛，不决定拿多少钱

**别拿"命中某能力的岗位薪资中位"当那个能力的价值**——那个数字主要反映
带宽切分，不是能力溢价。判据是**同带内对照**：固定 12-28k 后，
可观测/trace 命中组均值只比未命中组高 2.3k，而**公司规模从 20 人以下到
100 人以上直接差 5k**。

所以两个结论要分开用：
- **投递策略**看公司规模与融资阶段（这决定钱）
- **项目选题**看能力要求出现率（这决定能不能进面）
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

# 判定"真 agent/LLM 岗"。这份词表来自 1514 条人工核对过的样本，改之前先抽样验。
STRONG = re.compile(
    r"Agent|智能体|LLM|大模型|大语言模型|RAG|MCP|LangChain|LangGraph|Dify|Coze"
    r"|工作流编排|工具调用|AIGC|AI\s*原生", re.I)

# 场景 = "公司要拿 agent 干什么"，不按技术栈分。一条 JD 可命中多个。
SCENES: dict[str, str] = {
    "知识库问答": r"知识库|知识问答|文档问答|企业知识|智能问答",
    "客服/售后": r"客服|售后|工单|坐席|外呼|会话质检",
    "代码/研发效能": r"代码生成|研发效能|代码审查|CodeReview|编程助手|coding\s*agent|代码助手",
    "数据分析/BI": r"NL2SQL|Text2SQL|问数|BI|数据分析助手|自助取数",
    "电商/营销": r"电商|营销|选品|直播|种草|商品|广告投放|素材生成",
    "搜索推荐": r"搜索|召回|排序|推荐系统",
    "内容生成": r"文案|写作|创作|短视频|数字人|虚拟主播",
    "RPA/浏览器自动化": r"RPA|浏览器自动化|流程自动化|网页操作|爬虫",
    "AIOps/运维": r"运维|AIOps|告警|故障|根因|可观测|SRE",
    "语音/多模态": r"语音|ASR|TTS|多模态|OCR|视觉",
    "文档/合规质控": r"合同|档案|制度|规范审查|条款|质控|合规审查|文档解析",
    "医疗健康": r"医疗|病历|医院|临床|问诊",
    "教育": r"教育|教学|课程|作业|批改|题库",
    "金融/风控": r"金融|风控|信贷|保险|证券|银行",
    "招聘/HR": r"招聘|简历|HR|人才",
    "制造/工业": r"制造|工业|产线|质检|供应链",
    "硬件/具身": r"机器人|具身|嵌入式|硬件|IoT|物联网|ESP32|单片机|机械臂",
    "游戏": r"游戏|NPC|玩法",
}

# 能力 = 面试项目要覆盖的东西。
CAPS: dict[str, str] = {
    "RAG检索": r"RAG|向量检索|检索增强|embedding|重排|rerank|知识切片",
    "前端/全栈": r"React|Vue|前端|全栈|TypeScript",
    "工具调用/编排": r"工具调用|function\s*call|工作流编排|多智能体|multi-?agent|编排",
    "部署运维": r"Docker|K8s|Kubernetes|CI/CD|容器化|上线部署",
    "成本/性能": r"成本|token消耗|降本|延迟|首字|吞吐|QPS|缓存",
    "评测/eval": r"评测|评估集|eval|badcase|基准|benchmark|准确率|指标体系",
    "记忆/上下文": r"记忆|长期记忆|上下文工程|context|会话状态|多轮",
    "微调/训练": r"微调|SFT|LoRA|强化学习|RLHF|预训练|蒸馏",
    "MCP": r"MCP|Model Context Protocol",
    "权限/多租户": r"权限|多租户|数据隔离|鉴权|ACL|越权",
    "护栏/安全": r"幻觉|护栏|guardrail|注入|越狱|敏感词|内容安全",
    "自愈/重试": r"自愈|重试|降级|熔断|幂等|失败恢复",
    "可观测/trace": r"可观测|observability|链路追踪|trace|埋点|Metrics",
    "语音实时": r"ASR|TTS|实时语音|打断|barge|双向流",
    "人工审批HITL": r"人工审核|人工审批|人工介入|human.?in.?the.?loop|HITL",
    "低代码": r"Dify|Coze|低代码|扣子",
    "Prompt工程": r"Prompt\s*工程|提示词工程",
    "作品集认可": r"开源|GitHub|个人项目|作品集|技术博客|独立完成",
}


def median(vals: list[float]) -> float:
    vals = [v for v in vals if v]
    return statistics.median(vals) if vals else 0.0


def load(args) -> list[tuple[sqlite3.Row, str, float]]:
    con = sqlite3.connect(str(args.db), timeout=30.0)
    con.row_factory = sqlite3.Row
    rows: list[tuple[sqlite3.Row, str, float]] = []
    sql = ("SELECT title, company_name, salary_text, salary_max_k, company_size, "
           "       financing_stage, industry, experience, education, city, "
           "       job_description, glm_tags_json "
           "FROM jobs WHERE coalesce(job_description,'')!=''")
    for r in con.execute(sql):
        if args.match_filter:
            raw = r["glm_tags_json"]
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(d, dict) or d.get("match") != args.match:
                continue
        s = r["salary_max_k"] or 0.0
        if not (args.band[0] <= s <= args.band[1]):
            continue
        text = f"{r['title']} {r['job_description']}"
        if args.strong_only and not STRONG.search(text):
            continue
        rows.append((r, text, s))
    con.close()
    return rows


def show_scenes(rows) -> None:
    print(f"\n== 业务场景（一条 JD 可命中多个，故和 > 总数）")
    print(f"{'场景':<16}{'条数':>5}{'公司':>6}{'薪资中位':>9}")
    out = []
    for name, pat in SCENES.items():
        rs = [(r, s) for r, t, s in rows if re.search(pat, t, re.I)]
        if rs:
            out.append((len(rs), name, len({r["company_name"] for r, _ in rs}),
                        median([s for _, s in rs])))
    for n, name, comp, m in sorted(out, reverse=True):
        print(f"{name:<16}{n:>5}{comp:>6}{m:>8.0f}k")


def show_caps(rows) -> None:
    print(f"\n== 能力要求出现率（选题依据：这些是要覆盖的）")
    print(f"{'能力':<16}{'条数':>5}{'占比':>7}")
    out = []
    for name, pat in CAPS.items():
        n = len([1 for _, t, _ in rows if re.search(pat, t, re.I)])
        if n:
            out.append((n, name, n / len(rows)))
    for n, name, pct in sorted(out, reverse=True):
        print(f"{name:<16}{n:>5}{pct*100:>6.0f}%")


def show_attrib(rows) -> None:
    """薪资归因。**同带内对照**才能分清"能力溢价"与"带宽切分假象"。"""
    print(f"\n== 薪资归因：能力词的同带内对照（差值小 = 不是溢价来源）")
    print(f"{'能力':<16}{'n':>5}{'命中均值':>9}{'未命中':>8}{'差':>7}")
    out = []
    for name, pat in CAPS.items():
        hit = [s for _, t, s in rows if re.search(pat, t, re.I)]
        miss = [s for _, t, s in rows if not re.search(pat, t, re.I)]
        if len(hit) < 10 or not miss:
            continue
        out.append((statistics.mean(hit) - statistics.mean(miss), name,
                    len(hit), statistics.mean(hit), statistics.mean(miss)))
    for diff, name, n, h, m in sorted(out, reverse=True):
        print(f"{name:<16}{n:>5}{h:>8.1f}k{m:>7.1f}k{diff:>+6.1f}")

    print(f"\n== 薪资归因：公司属性（这才是钱的来源）")
    for field, label in (("company_size", "公司规模"),
                         ("financing_stage", "融资阶段"),
                         ("industry", "行业")):
        g: dict[str, list[float]] = collections.defaultdict(list)
        for r, _, s in rows:
            g[r[field] or "未标"].append(s)
        items = [(median(v), k, len(v)) for k, v in g.items() if len(v) >= 15]
        if not items:
            continue
        print(f"  {label}:")
        for m, k, n in sorted(items, reverse=True):
            print(f"    {k:<16}n={n:<5}中位={m:.0f}k")


def dump_jd(rows, keyword: str, limit: int) -> None:
    """打印命中 JD 原文片段。统计发现异常时用它核对，别只看聚合数字。"""
    pat = CAPS.get(keyword) or SCENES.get(keyword) or keyword
    rx = re.compile(pat, re.I)
    hits = sorted([(s, r, t) for r, t, s in rows if rx.search(t)],
                  key=lambda x: -x[0])
    print(f"\n== 命中 {keyword!r} 的 JD：{len(hits)} 条，按薪资降序取前 {limit}")
    for s, r, _ in hits[:limit]:
        print(f"\n### {r['title']} | {r['company_name']} | {r['salary_text']} "
              f"| {r['company_size']} {r['financing_stage']} | {r['industry']}")
        jd = re.sub(r"\s+", " ", r["job_description"])
        sents = [x.strip() for x in re.split(r"[。；\n]", jd) if rx.search(x)]
        for x in sents[:3]:
            print(f"   · {x[:190]}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", type=float, nargs=2, default=[12.0, 28.0],
                    metavar=("LO", "HI"),
                    help="薪资上限带（万/月，默认 %(default)s）。"
                         "这是可达性的第二道闸，不是筛高薪用的")
    ap.add_argument("--match", default="keep",
                    help="glm_tags.match 取值（keep/demote，默认 %(default)s）")
    ap.add_argument("--no-match-filter", dest="match_filter",
                    action="store_false",
                    help="不按 LLM 可达标签筛（会混入非应届岗，慎用）")
    ap.add_argument("--all-jobs", dest="strong_only", action="store_false",
                    help="不筛真 agent 岗（通用词噪音会很大）")
    ap.add_argument("--section", choices=["all", "scene", "caps", "attrib"],
                    default="all", help="只看某一节")
    ap.add_argument("--dump-jd", default="",
                    help="打印命中该能力/场景的 JD 原文（名字见上面两张表，也可直接给正则）")
    ap.add_argument("-n", type=int, default=10, help="--dump-jd 打印条数")
    ap.add_argument("--db", default=str(DB), help="数据库路径")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    rows = load(args)
    if not rows:
        print("没有命中任何岗位——检查 --band 与 --match 是否过严")
        sys.exit(1)
    scope = [f"薪资上限 {args.band[0]:.0f}-{args.band[1]:.0f}k"]
    if args.match_filter:
        scope.append(f"match={args.match}")
    if args.strong_only:
        scope.append("真 agent 岗")
    print(f"样本 {len(rows)} 条（{' ∩ '.join(scope)}），"
          f"薪资上限中位 {median([s for _, _, s in rows]):.0f}k")

    if args.dump_jd:
        dump_jd(rows, args.dump_jd, args.n)
        return
    if args.section in ("all", "scene"):
        show_scenes(rows)
    if args.section in ("all", "caps"):
        show_caps(rows)
    if args.section in ("all", "attrib"):
        show_attrib(rows)


if __name__ == "__main__":
    main()
