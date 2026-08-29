"""评估 match 判定质量：混淆矩阵 + 按类别的 precision/recall。

**为什么不看"一致率"**：一致率在类别不平衡下会骗人 —— 若样本里 90% 是 keep，
一个"全判 keep"的模型也有 90% 一致率，却把所有该踢的岗位都放进了投递池。

**这个场景该盯哪个指标**：投递池的代价是不对称的。
- 假阳性（该 demote 判成 keep）= 把要求 3 年经验的岗位放进池子 → 白投、污染排序
- 假阴性（该 keep 判成 demote）= 少看一个岗位 → 代价低得多

所以主指标是 **demote 的 recall**（该踢的里踢掉了几个）和 **keep 的 precision**
（放进池子的里有几个真能投）。2026-08-11 的对照实验里，候选模型 60 条 75% 一致率
听着还行，但换算成 demote recall 是 17/30 —— 灾难级，且那 13 条漏放的 match_reason 全为空。

用法:
  # 拿存量标签当基准，评估新模型/新 prompt（只读，不写库）
  uv run python scripts/eval_match_quality.py --model minimax-m3 --n 60

  # 只统计现有存量标签的分布（不调 API）
  uv run python scripts/eval_match_quality.py --stats-only
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "extract_glm_tags", ROOT / "scripts" / "extract_glm_tags.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def fmt_pct(num: int, den: int) -> str:
    return f"{num}/{den} ({num / den * 100:5.1f}%)" if den else f"{num}/0 (  n/a)"


def report(pairs: list[tuple[str, str]], flips: list[tuple]) -> None:
    """pairs = [(基准判定, 新判定), ...]。以 demote 为"该踢"的正类算指标。"""
    cm = Counter(pairs)
    # 基准 demote 且新也 demote = 正确踢掉
    dd = cm[("demote", "demote")]
    dk = cm[("demote", "keep")]     # ★ 漏放：最贵的错误
    kd = cm[("keep", "demote")]     # 误杀：代价低
    kk = cm[("keep", "keep")]
    total = dd + dk + kd + kk
    if not total:
        print("没有可比对的样本")
        return

    print(f"\n{'':14s}{'新判 demote':>12s}{'新判 keep':>12s}")
    print(f"{'基准 demote':14s}{dd:>12d}{dk:>12d}   ← dk 是漏放（最贵）")
    print(f"{'基准 keep':14s}{kd:>12d}{kk:>12d}")

    # keep precision 的分母是"新判 keep"的总数（kk + dk）：其中 dk 是被漏放进来的。
    print(f"\n一致率          {fmt_pct(dd + kk, total)}   ← 不平衡下会骗人，别单看这个")
    print(f"★ demote recall {fmt_pct(dd, dd + dk)}   该踢的里真踢掉了几个（漏放 {dk} 条）")
    print(f"★ keep precision{fmt_pct(kk, kk + dk)}   放进池子的里有几个真能投")
    print(f"  keep recall   {fmt_pct(kk, kk + kd)}   能投的里没被误杀几个（误杀 {kd} 条）")

    print("\n判据（投递池场景）：")
    print(f"  demote recall < 0.95 → 不可用（漏放会安静污染投递池，不报错）")
    print(f"  keep recall 低一些可接受（误杀只是多看一眼 JD）")

    if flips:
        print(f"\n判定翻转 {len(flips)} 条（需人工看谁对；空 reason 是漏判的特征）:")
        for title, old, new, reason, evid in flips[:15]:
            tag = " ⚠️空理由" if not str(reason or "").strip() else ""
            arrow = "漏放" if (old, new) == ("demote", "keep") else "误杀"
            print(f"  [{arrow}] {title[:26]:28s} {old:6s}→{new:6s}{tag}")
            print(f"         理由: {str(reason or '(空)')[:88]}")
            if evid:
                print(f"         证据: {str(evid)[:88]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="要评估的模型（默认读 .env 的 LLM_MODEL）")
    ap.add_argument("--n", type=int, default=60, help="样本数，keep/demote 各半")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    ap.add_argument("--stats-only", action="store_true", help="只看存量分布，不调 API")
    args = ap.parse_args()

    con = sqlite3.connect(ROOT / args.db if not Path(args.db).is_absolute() else args.db)
    con.row_factory = sqlite3.Row

    if args.stats_only:
        print("=== 存量标签的 match 分布 ===")
        for r in con.execute(
                "SELECT json_extract(glm_tags_json,'$.match') m, COUNT(*) c FROM jobs "
                "WHERE glm_tags_updated_at != '' GROUP BY 1 ORDER BY 2 DESC"):
            print(f"  {str(r['m']):8s} {r['c']}")
        print("\n=== 判定理由为空的比例（应为 0，非 0 说明有漏判风险）===")
        for want in ("keep", "demote"):
            tot = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE json_extract(glm_tags_json,'$.match')=?",
                (want,)).fetchone()[0]
            empty = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE json_extract(glm_tags_json,'$.match')=? "
                "AND COALESCE(TRIM(json_extract(glm_tags_json,'$.match_reason')),'')=''",
                (want,)).fetchone()[0]
            print(f"  {want:8s} 空理由 {fmt_pct(empty, tot)}")
        con.close()
        return

    env = _mod.load_env()
    if args.model:
        env["LLM_MODEL"] = args.model
    model = env.get("LLM_MODEL", "?")

    rows = []
    for want in ("keep", "demote"):
        rows += con.execute(
            "SELECT canonical_id, title, job_description, glm_tags_json FROM jobs "
            "WHERE glm_tags_updated_at != '' AND job_description != '' "
            "AND json_extract(glm_tags_json,'$.match') = ? "
            "ORDER BY random() LIMIT ?", (want, args.n // 2)).fetchall()
    con.close()
    print(f"评估 {model} vs 存量基准｜{len(rows)} 条（keep/demote 各半）｜并发 {args.concurrency}")

    pairs: list[tuple[str, str]] = []
    flips: list[tuple] = []
    failed = 0
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(_mod.call_glm, env, r["job_description"]): r for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            new, err, _ = fut.result()
            if not new:
                failed += 1
                continue
            old_m = (json.loads(r["glm_tags_json"] or "{}")).get("match", "?")
            new_m = new.get("match", "?")
            if old_m not in ("keep", "demote") or new_m not in ("keep", "demote"):
                failed += 1
                continue
            pairs.append((old_m, new_m))
            if old_m != new_m:
                flips.append((r["title"], old_m, new_m,
                              new.get("match_reason"), new.get("match_evidence")))

    print(f"调用失败 {failed}｜耗时 {time.monotonic() - t0:.0f}s")
    report(pairs, flips)


if __name__ == "__main__":
    main()
