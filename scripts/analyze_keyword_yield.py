"""关键词产出分析：哪些搜索词值得留，哪些在白烧列表请求。

判断一个词该不该留，看两个指标：
- **命中率** = 画像内岗位 / 总命中：低于 10% 说明被平台推荐算法带跑题
- **独占画像内** = 只有这个词能搜到的画像内岗位数：为 0 说明完全被其他词覆盖

## ⚠️ 两个口径修正（2026-08-11）

**① 配置不再写死 `config.hangzhou.yaml`**（原来写死，13 词）。
扩到 27 词后，14 个新词全被标成"已停用"——其中 `后端开发工程师` 命中率 76.7%、
独占画像内 177 条，是**全库最强的词**，却因为不在旧配置里而被打上停用标签。
现在默认读 `config.steady.yaml`，`--config` 可指定。

**② "无独占产出 → 该砍"这个判据在未翻透的词上会误杀**（这是本次核心修正）。
BOSS 的搜索结果是**浅层各词共享、深层各词独有**：前 1-3 页是所有相近词都返回的
热门岗位，第 4 页之后才是该词独有的长尾。一个只翻了 3 页的词，其独占产出必然
接近 0 —— 但那是**没翻到深层**，不是"被别的词覆盖"。

实测反例：`Python开发工程师` 页1-2 零新增，我一度判它"被 python 覆盖、该砍"，
结果页6→7条、页7→9条、页8→13条。现在它独占画像内 47 条。

所以本工具必须**结合翻页深度**判断，判据分三档：
- 翻页 ≥ DEEP_PAGE(6) 且独占为 0 → 真的被覆盖，可砍
- 翻页 < DEEP_PAGE 且独占为 0 → **数据不足，先翻深再评**（不给砍的建议）
- 命中率过低（<10%）→ 与深度无关，跑题就是跑题，可砍

用法:
    python scripts/analyze_keyword_yield.py
    python scripts/analyze_keyword_yield.py --config config.cities3.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "hangzhou_agent.sqlite3"
# 浅层共享、深层独有：翻不到第 6 页就没进过"该词独有"的区间，
# 此时独占产出为 0 是**采集深度不足**的正常现象，不能当成"被覆盖"。
DEEP_PAGE = 6
LOW_RATE = 10.0


def current_keywords(config: str) -> set[str]:
    """读配置里在用的关键词。裸 python 环境没装 pyyaml，退化为正则扫 yaml。"""
    try:
        from job_radar.config import load_config
        return set(load_config(config).keywords)
    except Exception:
        pass
    text = (ROOT / config).read_text(encoding="utf-8")
    block = re.search(r"^keywords:\n((?:\s*(?:#.*|-.*)\n)+)", text, re.M)
    if not block:
        return set()
    return {m.group(1).strip()
            for m in re.finditer(r"^\s*-\s*(.+?)\s*$", block.group(1), re.M)}


def load_progress(con: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """关键词 → (翻到的最大页, 是否有城市已确认翻到底)。

    多城市时取最大页：只要有一个城市翻深了，就说明这个词的深层长尾已被观测过。
    """
    out: dict[str, tuple[int, int]] = {}
    try:
        rows = con.execute(
            "SELECT keyword, MAX(last_page), MAX(exhausted) "
            "FROM keyword_progress GROUP BY keyword").fetchall()
    except sqlite3.OperationalError:
        return out
    for kw, page, exh in rows:
        out[kw] = (int(page or 0), int(exh or 0))
    return out


def verdict(in_config: bool, rate: float, own_act: int,
            page: int, exhausted: int) -> str:
    """三档判据。关键是把"被覆盖"和"没翻深"分开——前者该砍，后者该继续翻。"""
    if not in_config:
        return "未启用"
    if rate < LOW_RATE:
        # 跑题与深度无关：浅层深层都是跑题岗，翻得再深也不会变准。
        return f"⚠️ 命中率 {rate:.1f}% 过低，跑题，可砍"
    if own_act > 0:
        return "保留（有独占产出）"
    if exhausted:
        return "⚠️ 已翻到底且无独占产出，被其他词完全覆盖，可砍"
    if page < DEEP_PAGE:
        return f"？数据不足：只翻到第 {page} 页（深层未采），先翻深再评"
    return f"⚠️ 已翻到第 {page} 页仍无独占产出，可砍"


def main() -> None:
    ap = argparse.ArgumentParser()
    # 默认跟着"当前在用的配置"走。原来写死 hangzhou(13词)，
    # 导致扩容后的 14 个新词全被误标"已停用"。
    ap.add_argument("--config", default="config.steady.yaml")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_id, matched_queries_json, retention_status FROM jobs"
    ).fetchall()

    hit: dict[str, set[str]] = defaultdict(set)
    hit_active: dict[str, set[str]] = defaultdict(set)
    exclusive: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        try:
            queries = json.loads(r["matched_queries_json"] or "[]")
        except json.JSONDecodeError:
            queries = []
        queries = queries if isinstance(queries, list) else []
        for q in queries:
            hit[q].add(r["canonical_id"])
            if r["retention_status"] == "active":
                hit_active[q].add(r["canonical_id"])
        if len(queries) == 1:
            exclusive[queries[0]].add(r["canonical_id"])

    current = current_keywords(args.config)
    progress = load_progress(con)

    print(f"配置 {args.config}（{len(current)} 词）｜深层阈值 第 {DEEP_PAGE} 页")
    print(f"{'关键词':<22}{'总命中':>7}{'画像内':>7}{'命中率':>8}"
          f"{'独占':>6}{'独占画像内':>10}{'翻到页':>8}  判定")
    for k in sorted(hit, key=lambda x: -len(hit[x])):
        total, act = len(hit[k]), len(hit_active[k])
        own = exclusive.get(k, set())
        own_act = len(own & hit_active[k])
        rate = act / total * 100 if total else 0.0
        page, exh = progress.get(k, (0, 0))
        page_txt = f"{page}{'底' if exh else ''}" if page else "-"
        print(f"{k:<22}{total:>7}{act:>7}{rate:>7.1f}%{len(own):>6}{own_act:>10}"
              f"{page_txt:>8}  "
              f"{verdict(k in current, rate, own_act, page, exh)}")

    if current:
        never = sorted(current - set(hit))
        if never:
            print(f"\n配置里但库中无记录（未跑过或零结果，{len(never)} 个）: {never}")
        shallow = sorted(k for k in current
                         if k in hit and progress.get(k, (0, 0))[0] < DEEP_PAGE
                         and not progress.get(k, (0, 0))[1])
        if shallow:
            print(f"\n★ 尚未翻到深层（<第 {DEEP_PAGE} 页），产出被低估，"
                  f"续翻后再评估（{len(shallow)} 个）:\n  {shallow}")
    con.close()


if __name__ == "__main__":
    main()
