"""探针：候选免费模型与现役 stepfun 在 **match 判定**上的一致率。

换模型的风险不在"能不能输出 JSON"，而在**判定漂移**：
`match=keep/demote` 直接决定岗位进不进投递池，判错就是漏掉能投的岗、
或者把不能投的岗排到 P0。所以拿**已经打过标签的岗位**做对照，
比较新模型和存量标签的一致率。

只读不写：不碰 glm_tags_json，纯对照。
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

ROOT = Path(__file__).resolve().parent.parent.parent

spec = importlib.util.spec_from_file_location(
    "probe_zen_real", Path(__file__).resolve().parent / "probe_zen_real.py")
_pz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_pz)
call = _pz.call


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="laguna-s-2.1-free")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    con = sqlite3.connect(ROOT / "data" / "hangzhou_agent.sqlite3")
    con.row_factory = sqlite3.Row
    # 取已有标签的岗位，keep/demote 各半，避免样本偏向一边导致一致率虚高
    rows = []
    for want in ("keep", "demote"):
        rows += con.execute(
            "SELECT canonical_id, title, job_description, glm_tags_json FROM jobs "
            "WHERE glm_tags_updated_at != '' AND job_description != '' "
            "AND json_extract(glm_tags_json,'$.match') = ? "
            "ORDER BY random() LIMIT ?", (want, args.n // 2)).fetchall()
    con.close()
    print(f"对照 {args.model} vs 现役 stepfun｜{len(rows)} 条"
          f"（keep/demote 各半）｜并发 {args.concurrency}\n")

    agree = disagree = failed = 0
    conf: Counter = Counter()
    flips: list[tuple[str, str, str, str]] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(call, args.model, r["job_description"]): r for r in rows}
        for fut in as_completed(futs):
            r = futs[fut]
            new, err, _ = fut.result()
            old = json.loads(r["glm_tags_json"] or "{}")
            om = old.get("match", "?")
            if not new:
                failed += 1
                continue
            nm = new.get("match", "?")
            conf[f"old={om} new={nm}"] += 1
            if om == nm:
                agree += 1
            else:
                disagree += 1
                flips.append((r["title"][:30], om, nm,
                              (new.get("match_reason") or "")[:60]))

    total = agree + disagree
    print(f"一致 {agree}/{total}"
          f"（{agree / max(total, 1) * 100:.0f}%）｜不一致 {disagree}｜调用失败 {failed}"
          f"｜耗时 {time.monotonic() - t0:.0f}s")
    print("混淆分布:", dict(conf))
    if flips:
        print("\n判定翻转（前 12 条，需人工看谁对）:")
        for t, om, nm, why in flips[:12]:
            print(f"  {t:32s} {om:6s} → {nm:6s}  {why}")


if __name__ == "__main__":
    main()
