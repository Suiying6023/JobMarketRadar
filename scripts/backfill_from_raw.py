"""从 raw_json 零请求回填被 upsert 抹掉的详情专属列。

**为什么会有活可干**：`job_status_desc`/`position_name`/`recruitment_count` 长期
坐在 `OVERWRITE_COLUMNS` 里，而列表路径（`_parse_api_job`）从不赋值它们，于是每跑
一次纯列表采集就用空串抹一批。抹除率按 `seen_count` 单调爬升（seen=2 → 0%，
seen=8+ → 85%）。2026-08-11 已把这三列移进 `KEEP_IF_EMPTY_COLUMNS` 止血，
但**历史已抹的数据要从 `raw.detail` 里捞回来**——那份原始响应还在。

同时回填 `detail_fetched_at`：1468 行有 `raw.detail` 但只有 235 行有时间戳
（这列在 2026-08-10 之前全项目无写入点）。没有它就无法判断"详情该不该重抓"，
`models.py` 设计的详情过期重抓一直没能实现。

也回填 `district`：`areaDistrict` 为空时列表接口就不给，但 `address` 里写着
「杭州余杭区阿里中心·未科」，正则能抽出来。

零网络请求，可反复跑。默认 --dry-run 只报数。
用法：
    python scripts/backfill_from_raw.py            # 只看会改多少
    python scripts/backfill_from_raw.py --apply    # 真的写
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DB = "data/hangzhou_agent.sqlite3"

# 「杭州余杭区阿里中心」→ 余杭区；也兼容「西湖区」「滨江区」这类直接出现的
DISTRICT_RE = re.compile(r"([一-龥]{1,4}(?:区|县|市))")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只统计）")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_id, raw_json, job_status_desc, position_name, "
        "       recruitment_count, detail_fetched_at, district, address, city "
        "FROM jobs WHERE raw_json LIKE '%\"detail\"%'"
    ).fetchall()

    fixes: list[tuple] = []
    stats = {"job_status_desc": 0, "position_name": 0, "recruitment_count": 0,
             "detail_fetched_at": 0, "district": 0}

    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        detail = raw.get("detail") or {}
        job_info = detail.get("jobInfo") or {}
        if not job_info:
            continue

        sets: dict[str, object] = {}

        # 三个被抹的详情专属列：raw 里有值而列为空 → 回填
        for col, key in (("job_status_desc", "jobStatusDesc"),
                         ("position_name", "positionName"),
                         ("recruitment_count", "recruitmentCountDesc")):
            want = str(job_info.get(key) or "").strip()
            if want and not (r[col] or "").strip():
                sets[col] = want
                stats[col] += 1

        # detail_fetched_at：raw 有 detail 说明抓过详情，但时间戳缺失。
        # raw 里没有"何时抓的"这个信息，只能退而用 collected_at 近似——
        # 它是那次 upsert 的时间，抓详情必然伴随一次 upsert，误差在同一轮内。
        if not (r["detail_fetched_at"] or "").strip():
            approx = con.execute(
                "SELECT collected_at FROM jobs WHERE canonical_id = ?",
                (r["canonical_id"],)).fetchone()[0]
            if approx:
                sets["detail_fetched_at"] = approx
                stats["detail_fetched_at"] += 1

        # district：列表接口 areaDistrict 为空时不给，但 address 里有
        if not (r["district"] or "").strip():
            addr = str(r["address"] or job_info.get("address") or "")
            city = str(r["city"] or "")
            # 去掉城市名再找区，否则「杭州市」会被当成区
            m = DISTRICT_RE.search(addr.replace(city + "市", "").replace(city, ""))
            if m:
                sets["district"] = m.group(1)
                stats["district"] += 1

        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            fixes.append((f"UPDATE jobs SET {cols} WHERE canonical_id = ?",
                          (*sets.values(), r["canonical_id"])))

    print(f"扫描 {len(rows)} 行含 raw.detail 的记录，{len(fixes)} 行需要回填：")
    for k, v in stats.items():
        if v:
            print(f"  {k:22s} {v}")

    if not args.apply:
        print("\n--dry-run（默认）：未写库。加 --apply 生效")
        return

    for sql, params in fixes:
        con.execute(sql, params)
    con.commit()
    print(f"\n已写入 {len(fixes)} 行")

    # 复核
    print("回填后各列有值行数：")
    for col in ("job_status_desc", "position_name", "recruitment_count",
                "detail_fetched_at", "district"):
        n = con.execute(
            f"SELECT COUNT(*) FROM jobs WHERE trim(coalesce({col}, '')) <> ''"
        ).fetchone()[0]
        print(f"  {col:22s} {n}")


if __name__ == "__main__":
    main()
