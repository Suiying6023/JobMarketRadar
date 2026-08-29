"""本地数据分层：按画像把不符合的岗位标记为 sample（不物理删除）。

判定口径全部来自 `job_radar.profile`，本文件不自带阈值。当前包括：
城市白名单、匿名公司、薪资区间均值上限、日薪(实习)、经验/学历白名单、
代招、已失效/已关闭、标题与公司黑名单、招聘者头衔黑名单。

被降级为 sample 的记录保留在库中，作为后续增量扩充的去重样本——下次采集
遇到同一岗位（canonical_id 相同）走 UPDATE 而非 INSERT，避免重复抓取。
`bookmarked=1` 的岗位一律不动（人工决定优先于自动判定）。

用法:
  python scripts/clean_local_db.py --dry-run   # 只统计，不改
  python scripts/clean_local_db.py             # 把不符合的 active 标记为 sample
  python scripts/clean_local_db.py --purge     # 物理删除所有 sample 记录
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar import profile as P  # noqa: E402

DB = "data/hangzhou_agent.sqlite3"


def _raw_of(r) -> dict:
    """取 raw_json。老库的 raw 可能被列表轮次覆盖过，解析失败就返回空字典,
    让 is_anonymous 回退到公司名兜底判断。"""
    try:
        return json.loads(r["raw_json"] or "{}")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return {}


def should_demote(r) -> tuple[bool, str]:
    """画像判定。返回 (是否降级, 原因)。阈值/名单全部来自 job_radar.profile。

    关键词匹配一律走 `P.match_keyword()`——不要写
    `if any(kw in title for kw in KW): ... f"[{kw}]"`，生成器的 kw 不泄漏到
    外层作用域，那样写是 NameError 而不是匹配失败。
    """
    if r["job_invalid"]:
        return True, "已失效"
    if "关闭" in (r["job_status_desc"] or ""):
        return True, "已关闭"
    if r["is_proxy_job"]:
        return True, "代招"
    if not P.city_allowed(r["city"] or ""):
        return True, f"非目标城市[{r['city']}]"
    if P.is_anonymous(_raw_of(r), r["company_name"] or ""):
        return True, "匿名公司"
    if P.salary_out_of_range(r["salary_min_k"], r["salary_max_k"], r["salary_months"]):
        mid = P.salary_mid(r["salary_min_k"], r["salary_max_k"], r["salary_months"])
        return True, f"超纲(均值{mid:.0f}K)"
    if P.is_daily_salary(r["salary_min_k"], r["salary_max_k"]):
        return True, "日薪(实习)"
    exp = r["experience"] or ""
    if exp not in P.ALLOW_EXP:
        return True, f"经验[{exp}]"
    deg = r["education"] or ""
    if deg not in P.ALLOW_DEG:
        return True, f"学历[{deg}]"
    kw = P.match_keyword(r["title"] or "", P.EXCLUDE_TITLE)
    if kw:
        return True, f"标题[{kw}]"
    # OD 单独判：它要按词边界匹配，混进 EXCLUDE_TITLE（裸子串）会吃掉 Coding
    if P.outsourced_od(r["title"] or ""):
        return True, "外包[OD]"
    kw = P.match_keyword(r["company_name"] or "", P.EXCLUDE_COMPANY)
    if kw:
        return True, f"公司[{kw}]"
    kw = P.match_keyword(r["recruiter_title"] or "", P.EXCLUDE_RECRUITER_TITLE)
    if kw:
        return True, f"猎头发布[{kw}]"
    return False, ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计，不改")
    parser.add_argument("--purge", action="store_true", help="物理删除所有 sample 记录")
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    if args.purge:
        # 保住 bookmarked 的样本：那是用户手工浏览/投递过的岗位，物理删掉就找不回了
        n = con.execute(
            "DELETE FROM jobs WHERE retention_status = 'sample' AND bookmarked = 0"
        ).rowcount
        con.commit()
        print(f"已物理清理 {n} 条 sample 记录")
    else:
        # bookmarked=1 是用户手工判断（浏览/投递过），豁免自动降级
        rows = con.execute(
            "SELECT * FROM jobs WHERE retention_status = 'active' AND bookmarked = 0"
        ).fetchall()
        total = len(rows)
        to_demote: list[tuple[str, str, str]] = []
        reasons: dict[str, int] = {}
        for r in rows:
            del_flag, reason = should_demote(r)
            if del_flag:
                to_demote.append((r["canonical_id"], r["title"] or "", reason))
                reasons[reason] = reasons.get(reason, 0) + 1

        print(f"active 总量: {total}")
        print(f"将降级为 sample: {len(to_demote)}，保留 active: {total - len(to_demote)}")
        print(f"\n降级原因分布:")
        for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {n}")

        if args.dry_run:
            print("\n[DRY-RUN] 未执行标记。移除 --dry-run 即实际标记为 sample")
            print("\n降级样例（前 20 条）:")
            for cid, title, reason in to_demote[:20]:
                print(f"  [{reason}] {title[:36]}")
        else:
            con.execute("BEGIN")
            for cid, _t, _r in to_demote:
                con.execute(
                    "UPDATE jobs SET retention_status = 'sample' WHERE canonical_id = ?", (cid,)
                )
            con.commit()
            print(f"\n已标记 {len(to_demote)} 条为 sample")

    # 分层统计
    layers = {}
    for r in con.execute(
        "SELECT retention_status, bookmarked, COUNT(*) FROM jobs GROUP BY retention_status, bookmarked"
    ):
        key = f"{r[0]}" + (f"+bookmark" if r[1] else "")
        layers[key] = layers.get(key, 0) + r[2]
    pool = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE retention_status = 'active' OR bookmarked = 1"
    ).fetchone()[0]
    print(f"\n分层: {layers}")
    print(f"投递池(active OR bookmarked): {pool}")

    con.close()


if __name__ == "__main__":
    main()
