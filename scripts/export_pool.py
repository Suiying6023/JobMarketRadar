"""投递池导出：单文件 HTML，一个数据出口。

取代 `export_pool_html.py` 的七文件方案。那个方案把分桶做成了**文件边界**：
P0/P1/P2/高薪/低薪/超纲/风险公司 各一个 HTML，每个内嵌一份完整 JSON。
实测代价：
  · 七文件合计 **3.54 MB**，而全部 1656 条岗位的 JD 正文只有 0.42 MB
    —— 绝大部分体积是同一条岗位在多个文件里重复内嵌
  · 搜索、排序只在当前文件内生效，跨桶比较必须开两个页面手工对照
  · 「这条岗位在哪一页」本身成了要记住的事，而它只是几个字段的组合

这里把桶降级成**视图**：一份数据、一个页面，分桶靠客户端筛选。
体积更小、可跨桶搜索、切视图不重新加载。

用法：
    uv run python scripts/export_pool.py                 # 写 output/投递池.html
    uv run python scripts/export_pool.py --open          # 写完直接打开
    uv run python scripts/export_pool.py -o path/x.html  # 指定输出

判定口径全部复用 `export_pool_html` 里已验证的函数（`job_score`/`hard_drop`/
`salary_bucket`/`active_tier`/`real_risk_tags` 等），**不在这里重写阈值**——
两份判定会各自漂移，而漂移后无法判断页面上看到的是哪一套规则的结果。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar import profile as P  # noqa: E402
from job_radar.normalization import active_days_from_desc  # noqa: E402

# 复用已验证的判定与解析。这个模块同时也是旧七文件导出的实现，
# 两者共用同一套口径，只是呈现方式不同。
import export_pool_html as L  # noqa: E402
import _pool_template as T  # noqa: E402

DB = ROOT / "data" / "hangzhou_agent.sqlite3"

JOIN_ACTIONS = (
    "LEFT JOIN ("
    "  SELECT canonical_id,"
    "         MAX(CASE WHEN action='applied' THEN created_at END) applied_at,"
    "         MAX(CASE WHEN action='note' THEN message END) note"
    "  FROM chat_actions GROUP BY canonical_id"
    ") a ON a.canonical_id = j.canonical_id "
)


# 求职者本人是**统招全日制本科（双非）、2026 届应届**，所以这些要求他已经满足，
# 显示出来只是噪音。实测全库 gates 值里 506 条属于这一类（占一半），
# 满屏「本科及以上」会把真正该看的经验年限门槛淹掉。
_SATISFIED = re.compile(
    r"^(?:统招|全日制)?(?:本科|大专|专科|学历)(?:及以上|以上)?(?:学历)?$"
    r"|^经验不限$|^不限$|^学历不限$|^(?:接受)?应届|^在校"
)
# 「未提及」是 LLM 对"JD 没写这一项"的正常输出，不是标签。实测工作制字段
# 956 条都是它 —— 渲染出来等于满屏三个字的零信息量噪音。
_NOISE = ("未提及", "无", "不限", "未明确", "未说明", "-", "—")
_YEARS = re.compile(r"\d+\s*年|\d+\s*\+|至少\s*\d|以上经验")
_SCHOOL = re.compile(r"985|211|双一流|名校|知名院校|重点院校|重点高校|海外知名|QS")
_MASTER = re.compile(r"硕士|研究生|博士")


def is_noise(v: str) -> bool:
    """这个值是否不该出现在页面上（零信息量，或求职者已满足）。"""
    v = (v or "").strip()
    return (not v) or v in _NOISE or bool(_SATISFIED.match(v))


# 从整句里抠出门槛的**核心短语**。LLM 抽的 gates 是整句（实测中位 14 字、
# 89 条超 20 字、最长 40 字），铺上页面会把标签列撑变形——而句子里真正是门槛的
# 只有开头几个字：「1-3年真实Python开发经验，有内部业务系统…」的门槛是「1-3年」。
_G_YEARS = re.compile(
    r"(\d+\s*[-~–]\s*\d+|\d+)\s*年(?:半)?(?:以上|及以上|或以上|左右)?"
    r"|至少\s*(\d+)\s*年|(?:一|两|二|三|四|五|六|十)年(?:以上|及以上)?"
)
_G_SCHOOL = re.compile(r"985/?211|985|211|双一流|QS\s*前?\d*|名校|知名院校|重点院校|重点高校")
_G_MASTER = re.compile(r"硕士(?:及以上|以上)?|研究生(?:及以上|以上)?|博士")


def gate_label(v: str) -> str | None:
    """把一句门槛压成 ≤10 字的短标签。识别不出核心短语就返回 None。

    宁可丢掉也不铺整句：整句会撑破列宽，而完整原文在 JD 里一键可看。
    """
    m = _G_MASTER.search(v)
    if m:
        return m.group(0)[:8]
    m = _G_SCHOOL.search(v)
    if m:
        return m.group(0)[:10]
    m = _G_YEARS.search(v)
    if m:
        return m.group(0).replace(" ", "")[:8] + "经验"
    return None


def real_gates(g: dict) -> list[str]:
    """只留**真门槛**，且压成短标签：经验年限、名校、硕博。

    判据是这三类会让人投不进去，而「本科及以上」「经验不限」是已满足的条件。
    顺序按严重性：硕博与名校是绝对门槛，年限有时可放宽。
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in ("exp_req", "school_req", "edu_req", "other_req"):
        for v in L._as_list(g.get(key)):
            v = (v or "").strip()
            if is_noise(v):
                continue
            label = gate_label(v)
            if not label or label in seen:
                continue
            seen.add(label)
            out.append(label)
    return out[:3]


# 技术词表：从 LLM 抽的整句里**提取**已知技术词，而不是截断句子。
# 实测 `tech_req` 的值有 28% 超过 18 字（中位 10、最长 250），直接截断会产出
# 「深度学习框架（如TensorFlow」「保通用的编程语言能力，包括Pytho」这种
# 断句 —— 既读不懂又长短不齐，标签列没法纵向扫。
#
# 以 profile.AI_TECH 为核心（那 27 个词是画像评分在用的，必须一致），
# 再补通用技术栈：它们不参与评分，但决定"这岗位是什么技术方向"，
# 而那正是扫标签列时想知道的事。
_TECH_EXTRA = (
    "Java", "Go", "Golang", "C++", "TypeScript", "JavaScript", "Rust", "Kotlin",
    "React", "Vue", "Node.js", "Django", "Flask", "Spring Boot", "Spring Cloud",
    "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka",
    "Docker", "Kubernetes", "K8s", "Linux", "Git", "Nginx", "SQL",
    "TensorFlow", "Kubeflow", "HuggingFace", "Ollama", "vLLM", "Milvus",
    "Qdrant", "Chroma", "Faiss", "Pinecone", "LlamaIndex", "AutoGen", "CrewAI",
    "n8n", "Airflow", "Spark", "Flink", "Hadoop", "Pandas", "NumPy",
    "SSE", "WebSocket", "gRPC", "GraphQL", "RESTful", "OAuth",
    "Function Calling", "Tool Use", "ReAct", "CoT", "思维链", "知识图谱",
    "工作流", "多智能体", "强化学习", "SFT", "LoRA", "RLHF", "量化", "蒸馏",
    "爬虫", "分布式", "高并发", "微服务", "中间件", "云原生",
)
# 长词优先匹配，否则 "Spring" 会先命中而吃掉 "Spring Boot"。
_TECH_VOCAB = sorted(
    {*(t for t in _TECH_EXTRA), *(t for t in P.AI_TECH)},
    key=lambda s: -len(s),
)
# 展示时的规范写法（画像词表是小写的，直接上页面不好看）
_TECH_CANON = {
    "python": "Python", "llm": "LLM", "agent": "Agent", "rag": "RAG",
    "pytorch": "PyTorch", "langgraph": "LangGraph", "langchain": "LangChain",
    "fastapi": "FastAPI", "openai": "OpenAI", "transformer": "Transformer",
    "mcp": "MCP", "dify": "Dify", "coze": "Coze", "vllm": "vLLM",
    "milvus": "Milvus", "aigc": "AIGC", "function calling": "Function Calling",
    "prompt": "Prompt", "k8s": "K8s", "golang": "Go", "sql": "SQL",
}


# 泛词：出现频率高但区分度低。「大模型」几乎每条 AI 岗都有，而
# 「LangGraph」「Milvus」直接说明技术选型。标签位只有 6 个，
# 满了就要挤掉谁 —— 挤掉泛词，留下具体框架。
_TECH_GENERIC = frozenset({
    "大模型", "llm", "agent", "智能体", "深度学习", "机器学习", "多模态",
    "prompt", "分布式", "高并发", "微服务", "云原生", "中间件", "sql", "linux", "git",
})
# 标签列宽 340px 装得下几个标签：实测按字符宽估算，6 个有 31% 会换行、
# 5 个降到 11%、4 个几乎为 0。换行会把卡片高度从 ~44px 撑到 64px，
# 一屏少看三四条。取 5 —— 4 个丢的信息太多，5 个只有那些标签特别长的才换行。
_MAX_TECH = 5


def dedupe_tech(items) -> list[str]:
    """从整句里提取技术词，去重后返回短标签。

    只认词表里的词。代价是词表外的技术（冷门框架）不会显示，
    收益是每个标签都是**可读的短词**、长度一致、能纵向扫。
    在"完整"和"可读"之间选可读 —— 完整信息在 JD 正文里，一键可展开。

    ⚠️ 按信息量排序后再截断，不能按出现顺序截。原先边扫边截，
    结果第二句就填满 6 个位，`LangChain`/`LangGraph` 这类**框架名永远轮不到**
    （测试抓到的）—— 而它们比「大模型」这种泛词有用得多。
    """
    found: list[str] = []
    seen: set[str] = set()
    for v in items:
        if not isinstance(v, str):
            continue
        low = v.lower()
        for term in _TECH_VOCAB:
            tl = term.lower()
            if tl not in low:
                continue
            canon = _TECH_CANON.get(tl, term)
            k = canon.lower()
            if k in seen:
                continue
            # 只防完全包含（Go 被 Golang 覆盖），不合并不同概念
            if any(k != s and (k in s.split() or (len(k) <= 3 and k in s)) for s in seen):
                continue
            seen.add(k)
            found.append(canon)
    # 具体框架优先于泛词；同档内保持原顺序（越靠前说明 JD 越强调）
    found.sort(key=lambda t: t.lower() in _TECH_GENERIC)
    return found[:_MAX_TECH]


def row_to_record(r, bucket: str, tier: str, risky: bool) -> dict:
    """一条岗位 → 前端记录。字段名短是刻意的（1656 条 × 每个键都要重复一次）。

    `bucket`/`tier`/`risk` 就是原先的**文件归属**，现在降级成字段：
    前端按它们筛选即可，不必把同一条数据在多个文件里各存一份。
    """
    g = json.loads(r["glm_tags_json"] or "{}")
    reg = L.parse_reg(r)
    act = r["recruiter_active_status"] or ""
    act_days = r["recruiter_active_days"]
    if act_days is None:
        act_days = active_days_from_desc(act)
    tech = dedupe_tech(L._as_list(g.get("tech_req")))
    gates = real_gates(g)
    risks = L.real_risk_tags(reg)
    return {
        "id": r["canonical_id"],
        "t": r["title"] or "",
        "co": r["company_name"] or "",
        "url": r["job_url"] or "",
        "city": r["city"] or "",
        "dist": r["district"] or "",
        "sal": r["salary_text"] or "",
        "mid": round(L.salary_mid(r) or 0, 1),
        "score": L.job_score(r),
        # 技术栈原始命中数：同分排序的二级键。总分只有约 76 个可达值而池子近 800 条，
        # 99% 的岗位与他人同分 —— 没有二级键的话同分组内顺序等于 SQL 返回顺序。
        "techN": L.tech_hits(r),
        "jt": g.get("job_type") or "?",
        "size": r["company_size"] or "",
        "stage": r["financing_stage"] or "",
        "cap": L.extract_capital(r["company_intro"]) or reg.get("注册资本", ""),
        "found": reg.get("成立时间", ""),
        "regStatus": reg.get("经营状态", ""),
        # 结构化门槛：LLM 从 JD 抽出来的，这是页面上最该看的东西
        "tech": tech,
        "gates": gates,
        "why": (g.get("match_reason") or "")[:300],
        "match": g.get("match") or "",
        # 工作制只在 JD 真写了的时候才有意义：实测 956 条是「未提及」，
        # 渲染出来是满屏零信息量。双休是正面信号，非双休是负面，两者都要留；
        # 「未提及」丢掉 —— 它等于"这条 JD 没说"，不该占一个标签位。
        "sched": "" if is_noise(g.get("work_schedule") or "") else (g.get("work_schedule") or "")[:8],
        # 应届友好度同理：「接受应届」是默认预期（774 条），不值一个标签；
        # 只有「不接受」和「可放宽」才需要提示。
        "fresh": (lambda v: v if v and v != "接受应届" and not is_noise(v) else "")(
            g.get("fresh_grad") or ""),
        # 活跃度 + 它的快照年龄（"刚刚活跃"若是 20 天前记的就不可信）
        "act": act,
        "actDays": act_days,
        "age": L.observed_days_ago(r),
        "seen": r["last_seen_at"][:10] if r["last_seen_at"] else "",
        "verified": L.last_verified(r),
        # 分桶归属——原先是文件名，现在是可筛选字段
        "bucket": bucket,
        "tier": tier,
        "risk": risks,
        "applied": (r["applied_at"] or "")[:10],
        "note": r["note"] or "",
        "jd": r["job_description"] or "",
    }


def collect() -> tuple[list[dict], dict]:
    """读库 → 应用与旧导出**完全相同**的过滤分桶 → 扁平记录表 + 统计。"""
    from job_radar.analysis.market import connect_readonly
    con = connect_readonly(DB)
    con.row_factory = sqlite3.Row

    main_rows = con.execute(
        f"SELECT j.*, a.applied_at, a.note FROM jobs j {JOIN_ACTIONS}"
        "WHERE (j.retention_status='active' OR j.bookmarked=1) "
        "AND j.job_description != ''"
    ).fetchall()
    # 超纲岗在 sample 里（clean_local_db 按画像降的级，那个判断是对的），
    # 但仍要留档参考 —— 见 export_pool_html.main 的口径说明。
    over_rows = con.execute(
        f"SELECT j.*, a.applied_at, a.note FROM jobs j {JOIN_ACTIONS}"
        "WHERE j.retention_status='sample' AND j.bookmarked=0 "
        "AND j.job_description != '' AND j.job_invalid = 0 "
        # 与 P.salary_out_of_range 同口径：**月基薪**均值，不折算 salary_months。
        # 折算过的口径会把 `15-30K·14薪`（月基薪 22.5K）算成 26.2K 判超纲 ——
        # 而"够不着"看的是月薪能不能谈到，年终奖倍数不改变门槛。
        "AND j.salary_min_k IS NOT NULL AND j.salary_max_k IS NOT NULL "
        "AND (j.salary_min_k + j.salary_max_k) / 2.0 > ?",
        (P.SALARY_DROP_MID_ABOVE,),
    ).fetchall()
    con.close()

    drops: dict[str, int] = {}
    kept = []
    for r in main_rows:
        why = L.hard_drop(r)
        if why:
            drops[why] = drops.get(why, 0) + 1
        else:
            kept.append(r)
    kept, dup_n = L.dedupe(kept)

    recs = []
    for r in kept:
        risky = bool(L.real_risk_tags(L.parse_reg(r)))
        bucket = L.salary_bucket(r)
        # 风险公司原先被抽成独立文件、从主池移除。这里保留在同一份数据里，
        # 用 risk 字段标注 —— 页面默认过滤掉，但一键可看，不必换文件。
        tier = L.active_tier(r) if bucket == "core" else ""
        recs.append(row_to_record(r, bucket, tier, risky))
    over_extra = 0
    for r in over_rows:
        if not L.hard_drop(r):
            recs.append(row_to_record(r, "over", "", False))
            over_extra += 1

    stats = {
        "raw": len(main_rows),
        "dropped": sum(drops.values()),
        "dropReasons": drops,
        "dup": dup_n,
        "overArchive": over_extra,
        "date": date.today().isoformat(),
        "thresholds": {
            "salaryDropMid": P.SALARY_DROP_MID_ABOVE,
            "lowMax": P.SALARY_LOW_MID_MAX,
            "highMin": P.SALARY_HIGH_MID_MIN,
            "p0Days": P.ACTIVE_P0_MAX_DAYS,
            "staleFresh": P.STALE_FRESH_DAYS,
            "staleFull": P.STALE_FULL_DAYS,
            "cities": sorted(P.CITY_WHITELIST) if P.CITY_WHITELIST else ["全国"],
        },
    }
    return recs, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="output/投递池.html")
    ap.add_argument("--open", action="store_true", help="写完直接在浏览器打开")
    args = ap.parse_args()

    recs, stats = collect()
    thresholds = stats.pop("thresholds")
    dumps = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    html = T.build(dumps(recs), dumps(stats), dumps(thresholds))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    kb = out.stat().st_size / 1024
    by_bucket: dict[str, int] = {}
    for r in recs:
        by_bucket[r["bucket"]] = by_bucket.get(r["bucket"], 0) + 1
    tiers: dict[str, int] = {}
    for r in recs:
        if r["tier"]:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    print(f"原始 {stats['raw']} 条 → 硬过滤 {stats['dropped']} + 重复 {stats['dup']}")
    if stats["dropReasons"]:
        print("  硬过滤原因:", ", ".join(f"{k}={v}" for k, v in
                                     sorted(stats["dropReasons"].items(), key=lambda x: -x[1])))
    print(f"写入 {out}  {len(recs)} 条  {kb:.0f} KB")
    print(f"  分桶 {by_bucket}")
    print(f"  核心档分层 {tiers}  风险公司 {sum(1 for r in recs if r['risk'])} 条")

    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
