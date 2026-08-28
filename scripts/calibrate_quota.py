"""配额标定：在正确请求头下重新测量详情接口的风控行为。

**为什么需要这个脚本**：2026-08-10 发现 `fetch(url, headers)` 把 headers 当成 init
顶层字段传，被 fetch 白名单静默丢弃——此前**所有请求都没带任何自定义头**。于是
记忆/文档里那批配额数字（「详情 4-5 条/窗口」「code37 比例 ~20%」「封禁红线
150-200 条」「code37_limit=35」）全是**裸配额**下测出来的，带上正确的
`zp_token` + `x-requested-with` 后行为可能完全不同，放量前必须重标定。

**和 backfill_details 的区别**：那个脚本目标是补数据，节流参数按"安全优先"配死；
这个脚本目标是**测量**，所以：
1. 每条请求单独记录耗时/结果/code，输出一份可对比的 JSONL
2. 阶梯式提速：前 N 条用保守间隔，之后逐档缩短，找到 code=37 首次出现的位置
3. 任何风控信号（code=37 之外的一切）立即停止并落盘
4. 严格的硬上限（默认 20 条），到量就停，不让"再试一条"变成滑坡

抓到的详情数据照常写库（标定顺便补数据，不浪费请求）。

用法：
    python scripts/calibrate_quota.py --limit 12            # 标定 12 条
    python scripts/calibrate_quota.py --limit 12 --dry-run  # 只看选到哪些，不发请求
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar.collectors.boss import (  # noqa: E402
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

# 阶梯：(累计到第几条为止, 条内间隔秒). 前段保守，逐档提速找 code=37 阈值。
# 起点 [8,12] 就是当前 config 的生产档——先确认生产档在新头下是干净的，
# 再往下压。反过来（从激进开始）会在还不知道基线的情况下先烧掉信用。
LADDER: tuple[tuple[int, tuple[float, float]], ...] = (
    (4, (10.0, 14.0)),
    (8, (7.0, 10.0)),
    (12, (5.0, 7.0)),
    (16, (3.5, 5.0)),
    (999, (2.5, 3.5)),
)


def ladder_delay(idx: int) -> tuple[float, float]:
    for upto, delay in LADDER:
        if idx <= upto:
            return delay
    return LADDER[-1][1]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml")
    parser.add_argument("--limit", type=int, default=12,
                        help="硬上限，到量即停（默认 12，谨慎起步）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出候选，不发任何请求")
    args = parser.parse_args()

    cfg = load_config(args.config)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"calib-{stamp}.jsonl"

    with JobStore(cfg.database_path) as store:
        # 候选优先选**真正需要详情的 active 岗位**：标定必须花请求，那就让这些请求
        # 顺便补上有用数据。优先级：活跃度空 > raw 缺 detail > 无 JD。
        rows = store.conn.execute(
            """
            SELECT * FROM jobs
            WHERE retention_status = 'active' AND job_invalid = 0
              AND raw_json != '{}'
              AND (
                    recruiter_active_status IS NULL OR recruiter_active_status = ''
                 OR raw_json NOT LIKE '%"detail"%'
                 OR job_description IS NULL OR job_description = ''
              )
            ORDER BY
              CASE WHEN recruiter_active_status IS NULL
                     OR recruiter_active_status = '' THEN 0 ELSE 1 END,
              canonical_id
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()

        today = store.request_counts()
        print(f"今日账本：{today}")
        print(f"候选 {len(rows)} 条（硬上限 {args.limit}）")
        for r in rows[:5]:
            print(f"  - {r['title'][:28]} | 活跃度={r['recruiter_active_status'] or '空'}")
        if args.dry_run:
            print("\n--dry-run：不发请求，退出")
            return
        if not rows:
            print("没有候选，退出")
            return

        collector = BossCollector(cfg)
        collector.ledger = store
        collector.ledger_script = "calibrate_quota"
        await collector.start()
        first_city = cfg.cities[0]
        first_filter = cfg.filters[0] if cfg.filters else SearchFilters()
        nav_params = collector._params(first_city, cfg.keywords[0], first_filter, 1)
        nav_visible = {k: v for k, v in nav_params.items()
                       if k not in ("pageSize", "scene")}
        from urllib.parse import urlencode as _urlencode
        await collector._goto_search(f"{SEARCH_BASE}?{_urlencode(nav_visible)}")
        print(f"已导航: {collector.page.url[:80]}\n")

        events: list[dict] = []
        done = 0
        failed = 0
        stop_reason = "完成全部候选"
        try:
            for idx, row in enumerate(rows, start=1):
                lo, hi = ladder_delay(idx)
                # 直接改 config 让 _collect_detail 内建的条内延迟走标定阶梯，
                # 不另写一套延迟逻辑（否则测的就不是生产代码路径了）。
                cfg.detail_delay_seconds = [lo, hi]
                job = _row_to_job(row)
                before37 = collector.code37_count
                t0 = time.monotonic()
                event = {
                    "idx": idx,
                    "canonical_id": row["canonical_id"],
                    "title": row["title"][:40],
                    "delay_band": [lo, hi],
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop as exc:
                    event.update(result="RISK_STOP", error=str(exc)[:200])
                    events.append(event)
                    stop_reason = f"命中风控：{exc}"
                    break
                except Exception as exc:
                    failed += 1
                    event.update(result="error", error=str(exc)[:200])
                    events.append(event)
                    print(f"[{idx}] 失败：{str(exc)[:80]}", flush=True)
                    continue
                store.upsert(job)
                done += 1
                event.update(
                    result="ok",
                    elapsed=round(time.monotonic() - t0, 1),
                    code37_delta=collector.code37_count - before37,
                    active_status=job.recruiter_active_status,
                    active_days=job.recruiter_active_days,
                    jd_len=len(job.job_description or ""),
                )
                events.append(event)
                flag = " ⚠️code37" if event["code37_delta"] else ""
                print(
                    f"[{idx}] ok 间隔{lo:.0f}-{hi:.0f}s 用时{event['elapsed']}s "
                    f"活跃度={job.recruiter_active_status or '空'} "
                    f"JD={event['jd_len']}字{flag}",
                    flush=True,
                )
        except RiskControlStop as exc:
            stop_reason = f"命中风控：{exc}"
        finally:
            out_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
                encoding="utf-8",
            )
            await collector.close()

        print(f"\n{'=' * 56}")
        print(f"停止原因: {stop_reason}")
        print(f"成功 {done} / 失败 {failed} / 请求 {collector.request_count} 次")
        print(f"code=37 重算 {collector.code37_count} 次")
        print(f"分类计数: {collector.kind_counts}")
        print(f"今日账本: {store.request_counts()}")
        print(f"明细已存: {out_path}")

        first37 = next((e["idx"] for e in events if e.get("code37_delta")), None)
        if first37:
            print(f"\n⚠️ code=37 首次出现在第 {first37} 条"
                  f"（间隔档 {ladder_delay(first37)}）")
        elif done:
            print(f"\n✅ {done} 条全程零 code=37，最快档到 {ladder_delay(done)}")


if __name__ == "__main__":
    asyncio.run(main())
