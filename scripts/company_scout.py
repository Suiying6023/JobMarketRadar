"""从 JD 库筛"值得调研业务的公司"，并导出业务线索供并行调研。

**为什么需要这一步**：JD 里的职责清单高度同质（都写 RAG/LangChain/Prompt），
看不出公司要拿 agent 干什么。而**公司简介 + JD 里的业务背景段**才是场景原料 ——
「跨境卖家的选品洞察与达人建联」「国际物流全链路履约」这类信息只在那里。

用法：
    uv run python scripts/company_scout.py                    # 全部候选概览
    uv run python scripts/company_scout.py --group ecom       # 只看某行业组
    uv run python scripts/company_scout.py --export DIR       # 按组导出成文件
    uv run python scripts/company_scout.py --company 运去哪    # 打印某公司完整 JD
    uv run python scripts/company_scout.py --min-posts 2      # 只看招多个岗的

## 四道筛选（每道都有理由）

1. **可达**：`glm_tags.match=keep` ∩ 真 agent 词表 ∩ 薪资上限在带内。
2. **排除脱敏名**：「某中型人工智能公司」这类查不到真实业务，调研不出场景。
3. **排除外包/人力派驻**：万宝盛华、软通动力、法本这类公司的业务是"给别人干活"，
   它的 JD 描述的是甲方的活，反推不出自有场景。
4. **优质信号**：规模 >=100 人 **或** A轮以上/已上市。小微未融资公司的 agent 岗
   多是"拿 agent 当噱头"，且薪资中位低 5k（见 [[agent-jd-market-signals]] 的归因）。

**同一家招多个 agent 岗是最强的"真投入"信号**（`--min-posts 2`）：
它意味着公司在建整条线，不是试水一个人。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

STRONG = re.compile(
    r"Agent|智能体|LLM|大模型|大语言模型|RAG|MCP|LangChain|LangGraph|Dify|Coze"
    r"|工作流编排|工具调用|AIGC|AI\s*原生", re.I)

# 脱敏名：查不到真实业务
ANON = re.compile(r"^某|某(大型|中型|小型|知名|头部)|^[A-Z]{1,3}公司$")
# 外包/人力派驻：JD 描述的是甲方的活
OUTSOURCE = re.compile(r"人力资源|外企德科|万宝盛华|软通动力|法本|诚迈|派遣"
                       r"|人才服务|猎头|外包|инфо")

GOOD_SIZE = {"100-499人", "500-999人", "1000-9999人", "10000人以上"}
GOOD_STAGE = {"A轮", "B轮", "C轮", "D轮及以上", "已上市", "战略融资"}

# 行业分组：按"业务形态"分，便于把相近业务交给同一个调研 agent
GROUPS: dict[str, str] = {
    "ecom": r"电子商务|电商|零售|跨境|广告营销|互联网金融|新零售",
    "saas": r"计算机软件|企业服务|大数据|计算机服务|云计算",
    "hw": r"智能硬件|计算机硬件|半导体|通信/网络设备|电子/硬件|其他制造业"
          r"|新材料|机器人|汽车",
    "content": r"在线教育|社交网络|媒体|游戏|文化|广告",
    "vertical": r"医疗健康|医疗|法律|物流|建筑|能源|农业|旅游|基金|证券",
    "ai": r"人工智能",
    "net": r"^互联网$",
}


def median(v: list[float]) -> float:
    v = [x for x in v if x]
    return statistics.median(v) if v else 0.0


def load(args) -> list[dict]:
    con = sqlite3.connect(args.db, timeout=30.0)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute(
            "SELECT title,company_name,salary_text,salary_max_k,company_size,"
            "financing_stage,industry,company_intro,job_description,glm_tags_json "
            "FROM jobs WHERE coalesce(job_description,'')!=''"):
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
        if not STRONG.search(f"{r['title']} {r['job_description']}"):
            continue
        rows.append(r)
    con.close()

    by_co: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        by_co[r["company_name"]].append(r)

    cands = []
    for co, items in by_co.items():
        if not args.keep_anon and ANON.search(co):
            continue
        if not args.keep_outsource and OUTSOURCE.search(co):
            continue
        r0 = items[0]
        size, stage = r0["company_size"] or "", r0["financing_stage"] or ""
        if args.quality_only and not ((size in GOOD_SIZE) or (stage in GOOD_STAGE)):
            continue
        if len(items) < args.min_posts:
            continue
        intro = re.sub(r"\s+", " ", r0["company_intro"] or "")
        if len(intro) < args.min_intro:
            continue
        cands.append({
            "co": co, "n": len(items), "size": size, "stage": stage,
            "industry": r0["industry"] or "",
            "sal": median([x["salary_max_k"] for x in items]),
            "titles": sorted({x["title"] for x in items}),
            "salaries": sorted({x["salary_text"] for x in items}),
            "intro": intro,
            "jds": [re.sub(r"\s+", " ", x["job_description"]) for x in items],
        })
    return cands


def biz_段(jd: str) -> str:
    """抓 JD 里的业务背景段——职责清单同质化，业务段才有场景信息。"""
    m = re.search(r"(团队介绍|团队使命|关于我们|公司介绍|我们(正在|专注|相信)"
                  r"|业务背景|产品(介绍|形态)|【团队)(.{40,400})", jd)
    return m.group(0) if m else ""


def show(cands: list[dict], verbose: bool) -> None:
    print(f"{'公司':<22}{'岗':>3}{'中位':>6}  {'规模':<12}{'阶段':<9}行业")
    print("-" * 78)
    for c in sorted(cands, key=lambda x: (-x["n"], -x["sal"])):
        print(f"{c['co'][:21]:<22}{c['n']:>3}{c['sal']:>5.0f}k  "
              f"{c['size']:<12}{c['stage'] or '—':<9}{c['industry'][:14]}")
        if verbose:
            print(f"    岗位: {' / '.join(c['titles'][:5])}")
            print(f"    简介: {c['intro'][:280]}")
            seg = biz_段(c["jds"][0])
            if seg:
                print(f"    业务段: {seg[:280]}")
            print()


def export(cands: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for gname, pat in GROUPS.items():
        rx = re.compile(pat, re.I)
        group = [c for c in cands if rx.search(c["industry"])]
        if not group:
            continue
        path = out_dir / f"companies-{gname}.txt"
        with path.open("w", encoding="utf-8") as f:
            f.write(f"分组 {gname}：{len(group)} 家\n\n")
            for c in sorted(group, key=lambda x: (-x["n"], -x["sal"])):
                f.write(f"### {c['co']} | {c['n']}岗 中位{c['sal']:.0f}k | "
                        f"{c['size']} {c['stage'] or '—'} | {c['industry']}\n")
                f.write(f"    岗位: {' / '.join(c['titles'][:5])}\n")
                f.write(f"    薪资: {' / '.join(c['salaries'][:5])}\n")
                f.write(f"    简介: {c['intro'][:400]}\n")
                seg = biz_段(c["jds"][0])
                if seg:
                    f.write(f"    业务段: {seg[:400]}\n")
                f.write("\n")
        print(f"{path}  ({len(group)} 家)")


def dump_company(args, name: str) -> None:
    con = sqlite3.connect(args.db, timeout=30.0)
    con.row_factory = sqlite3.Row
    n = 0
    for r in con.execute(
            "SELECT title,company_name,salary_text,company_size,financing_stage,"
            "industry,company_intro,job_description FROM jobs "
            "WHERE company_name LIKE ? AND coalesce(job_description,'')!=''",
            (f"%{name}%",)):
        n += 1
        print(f"\n{'='*78}")
        print(f"{r['company_name']} | {r['title']} | {r['salary_text']} "
              f"| {r['company_size']} {r['financing_stage']} | {r['industry']}")
        print(f"{'='*78}")
        intro = re.sub(r"\s+", " ", r["company_intro"] or "")
        if intro:
            print(f"[简介] {intro[:500]}\n")
        print(re.sub(r"\s*\n\s*", "\n", r["job_description"])[:3000])
    con.close()
    if not n:
        print(f"没找到公司名含 {name!r} 的岗位")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", type=float, nargs=2, default=[12.0, 28.0],
                    metavar=("LO", "HI"), help="薪资上限带（默认 %(default)s）")
    ap.add_argument("--match", default="keep", help="glm_tags.match 取值")
    ap.add_argument("--no-match-filter", dest="match_filter",
                    action="store_false", help="不按 LLM 可达标签筛")
    ap.add_argument("--group", choices=sorted(GROUPS), help="只看某行业组")
    ap.add_argument("--min-posts", type=int, default=1,
                    help="至少招几个可达 agent 岗（>=2 是真投入信号）")
    ap.add_argument("--min-intro", type=int, default=30,
                    help="公司简介至少多少字（太短的没有业务线索）")
    ap.add_argument("--keep-anon", action="store_true",
                    help="保留脱敏公司名（默认排除，因为查不到业务）")
    ap.add_argument("--keep-outsource", action="store_true",
                    help="保留外包/人力派驻公司（默认排除）")
    ap.add_argument("--all-companies", dest="quality_only",
                    action="store_false",
                    help="不筛优质信号（默认只留 >=100人 或 A轮以上）")
    ap.add_argument("--export", metavar="DIR", help="按行业组导出成文件")
    ap.add_argument("--company", help="打印该公司的完整 JD 原文")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="同时打印简介与业务段")
    ap.add_argument("--db", default=str(DB), help="数据库路径")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    if args.company:
        dump_company(args, args.company)
        return

    cands = load(args)
    if args.group:
        rx = re.compile(GROUPS[args.group], re.I)
        cands = [c for c in cands if rx.search(c["industry"])]
    if not cands:
        print("没有候选——放宽 --band / --min-posts / --min-intro 再试")
        sys.exit(1)

    scope = [f"上限 {args.band[0]:.0f}-{args.band[1]:.0f}k"]
    if args.match_filter:
        scope.append(f"match={args.match}")
    if args.quality_only:
        scope.append("规模>=100人或A轮以上")
    if args.min_posts > 1:
        scope.append(f">={args.min_posts}岗")
    print(f"候选 {len(cands)} 家（{' ∩ '.join(scope)}）\n")

    if args.export:
        export(cands, Path(args.export))
    else:
        show(cands, args.verbose)


if __name__ == "__main__":
    main()
