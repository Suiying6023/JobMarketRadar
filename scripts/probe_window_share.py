"""验证列表接口和详情接口是否共享同一个 code=37 配额窗口。

**为什么值得单独测**：两处独立观察到同一个「约 5」这个数字——
- 详情：2026-08-10 标定 43 条，code=37 间距 [6,5,6,6,4,5] 平均 5.3 条
- 列表：同日 expand_list 37 页触发 6 次 code=37，约每 6 页一次

如果两个接口**共享**一个窗口，那"列表很安全、详情才危险"这个长期认知就是错的，
混跑时的实际风险要按请求总数算（而不是分开算两笔预算）；采集策略也得改——
现在 collect() 是"列表一页 → 该页所有 active 岗位的详情"交替发，正好是最坏情况。
如果**独立**，则列表可以放心快抓，详情单独节流。

**实验设计**：交替 列表→详情→列表→详情…，记录每次 code=37 落在哪个接口上。
- 共享窗口 → code=37 应该每约 5 次**总请求**出现一次，且列表/详情都可能触发
- 独立窗口 → 各自要累到约 5 次**同类请求**才触发，即约每 10 次总请求一次

对照组用同日已有数据：纯详情（间距 5.3）、纯列表（间距 ~6）。

用法: python scripts/probe_window_share.py --rounds 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import (  # noqa: E402
    API_PATH,
    SEARCH_BASE,
    BossCollector,
    RiskControlStop,
)
from job_radar.config import load_config  # noqa: E402
from job_radar.models import SearchFilters  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_details import _row_to_job  # noqa: E402

OUT_DIR = Path("data/calibration")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--rounds", type=int, default=10,
                        help="交替轮数，每轮 = 1 次列表 + 1 次详情")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # 实验期间用固定短间隔：标定已证明间隔不影响 code=37 位置，
    # 这里要测的是"计数怎么累"，间隔只影响总耗时。
    cfg.detail_delay_seconds = [4.0, 6.0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"window-share-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    with JobStore(cfg.database_path) as store:
        # 详情素材：sample 池无 JD 的（active 已无待补），顺带补数据
        rows = store.conn.execute(
            "SELECT * FROM jobs WHERE trim(coalesce(job_description,'')) = '' "
            "AND raw_json != '{}' AND job_invalid = 0 "
            "ORDER BY canonical_id LIMIT ?",
            (args.rounds,),
        ).fetchall()
        if len(rows) < args.rounds:
            print(f"⚠️ 详情素材只有 {len(rows)} 条，轮数降到该值")
            args.rounds = len(rows)
        if not args.rounds:
            print("无详情素材，退出")
            return

        print(f"今日账本：{store.request_counts()}")
        print(f"交替 {args.rounds} 轮（每轮 1 列表 + 1 详情 = {args.rounds * 2} 次请求）\n")

        collector = BossCollector(cfg)
        collector.ledger = store
        collector.ledger_script = "probe_window_share"
        await collector.start()
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else SearchFilters()
        nav = collector._params(city, cfg.keywords[0], filters, 1)
        await collector._goto_search(
            f"{SEARCH_BASE}?{urlencode({k: v for k, v in nav.items() if k not in ('pageSize', 'scene')})}"
        )
        print(f"已导航: {collector.page.url[:70]}\n")

        events: list[dict] = []
        seq = 0  # 总请求序号（含两类）
        try:
            for rnd in range(1, args.rounds + 1):
                # --- 列表请求 ---
                # 每轮换一个关键词+页码，避免命中平台的重复查询缓存
                kw = cfg.keywords[rnd % len(cfg.keywords)]
                page_no = (rnd % 5) + 1
                params = collector._params(city, kw, filters, page_no)
                if collector.last_lid:
                    params["lid"] = collector.last_lid
                before = collector.code37_count
                seq += 1
                try:
                    body = await collector._same_origin_fetch(
                        f"{API_PATH}?{urlencode(params)}", recover_code37=True
                    )
                    n_jobs = len(((body.get("zpData") or {}).get("jobList") or []))
                    zp = body.get("zpData") or {}
                    if zp.get("lid"):
                        collector.last_lid = str(zp["lid"])
                    delta = collector.code37_count - before
                    events.append({"seq": seq, "round": rnd, "kind": "list",
                                   "code37": delta, "n_jobs": n_jobs, "keyword": kw})
                    print(f"[{seq:2d}] list   {kw[:12]:12s} 页{page_no} "
                          f"{n_jobs:2d}条{' ⚠️code37' if delta else ''}", flush=True)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    events.append({"seq": seq, "round": rnd, "kind": "list",
                                   "error": str(exc)[:120]})
                    print(f"[{seq:2d}] list   失败: {str(exc)[:60]}", flush=True)

                await asyncio.sleep(3.0)

                # --- 详情请求 ---
                job = _row_to_job(rows[rnd - 1])
                before = collector.code37_count
                seq += 1
                try:
                    job = await collector._collect_detail(job)
                    store.upsert(job)
                    delta = collector.code37_count - before
                    events.append({"seq": seq, "round": rnd, "kind": "detail",
                                   "code37": delta, "jd_len": len(job.job_description or "")})
                    print(f"[{seq:2d}] detail {job.title[:20]:20s} "
                          f"JD={len(job.job_description or '')}字"
                          f"{' ⚠️code37' if delta else ''}", flush=True)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    events.append({"seq": seq, "round": rnd, "kind": "detail",
                                   "error": str(exc)[:120]})
                    print(f"[{seq:2d}] detail 失败: {str(exc)[:60]}", flush=True)
        except RiskControlStop as exc:
            print(f"\n命中风控停止：{exc}")
        finally:
            out_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
                encoding="utf-8")
            await collector.close()

        # ---- 判读 ----
        print(f"\n{'=' * 58}")
        hits = [e for e in events if e.get("code37")]
        print(f"总请求 {seq} 次，code=37 共 {len(hits)} 次")
        print(f"分类计数: {collector.kind_counts}")
        if hits:
            print("\ncode=37 落点（总请求序号 / 接口）:")
            prev = 0
            for h in hits:
                print(f"  seq={h['seq']:2d} ({h['kind']})  距上次 {h['seq'] - prev} 次总请求")
                prev = h["seq"]
            kinds = {h["kind"] for h in hits}
            avg = seq / len(hits)
            print(f"\n平均每 {avg:.1f} 次总请求触发一次")
            print(f"触发接口: {kinds}")
            print("\n判读：")
            if len(kinds) > 1:
                print("  ✅ 两个接口都触发过 → 强烈支持**共享同一窗口**")
            if avg <= 7:
                print(f"  ✅ 每 {avg:.1f} 次总请求一次，接近纯详情的 5.3 条 → **共享**")
                print("     （若独立，混跑时应约每 10 次总请求才一次）")
            else:
                print(f"  ⚠️ 每 {avg:.1f} 次总请求一次，明显稀于纯详情的 5.3")
                print("     → 倾向**独立窗口**（各自累计）")
        else:
            print("零 code=37 —— 样本太小或窗口刚被重置，无法判读")
        print(f"\n明细: {out_path}")
        print(f"今日账本: {store.request_counts()}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
