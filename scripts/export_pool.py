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
    tech = L._as_list(g.get("tech_req"))[:6]
    gates = (L._as_list(g.get("exp_req"))[:2] + L._as_list(g.get("edu_req"))[:1]
             + L._as_list(g.get("school_req"))[:1])
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
        "sched": g.get("work_schedule") or "",
        "fresh": g.get("fresh_grad") or "",
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
    con = sqlite3.connect(DB)
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
        "AND j.salary_min_k IS NOT NULL AND j.salary_max_k IS NOT NULL "
        "AND (j.salary_min_k + j.salary_max_k) / 2.0 "
        "    * COALESCE(j.salary_months, 12) / 12 > ?",
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
