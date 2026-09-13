"""封禁边界探测：一直抓到 code=32/36 为止，记下"封禁发生在第几次请求"。

用户三次明确授权（"可以继续扩大数据库直到账号被封禁"/"日预算也可以突破"/
"可以尝试激进一些试探封禁边界"）。

**为什么必须专门写一个脚本**：封禁**没有渐进信号**。
- code=37 密度不随累计量上升（2026-08-11 实测 407 次请求，间距全程稳定 6.0）
  → 它是无记忆滑动窗口，**无法预警封禁**
- 历史反例很硬：08-03「156 条仅 1 次 code=37 就直接 code=32」，
  另一次「108 条 21 次 code=37 零封禁」→ 封禁与 code=37 频率无关
- 封禁是累计量 + 账号信任度的另一套机制，会直接返回 code=32
所以唯一能采到的数据点是「今天第 N 次请求时被封」，必须在被封的那一刻
把上下文完整落盘，否则这次代价（次日不可采集）就白付了。

**输出**：`data/calibration/ban-probe-<时间>.json`
包含封禁时的累计请求数、当日起始基线、各阶段用量、封禁正文、code=37 总数。

用法：
    python scripts/probe_ban_boundary.py                    # 抓到封禁为止
    python scripts/probe_ban_boundary.py --max-details 400  # 或到量为止
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from job_radar.collectors.boss import (  # noqa: E402
    SEARCH_BASE,
    BossCollector,
    RiskControlStop,
)
from job_radar.config import load_config  # noqa: E402
from job_radar.models import SearchFilters  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

from backfill_details import _row_to_job  # noqa: E402

OUT_DIR = ROOT / "data" / "calibration"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.aggressive.yaml")
    ap.add_argument("--max-details", type=int, default=1000,
                    help="安全兜底上限（正常应由封禁终止）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"ban-probe-{stamp}.json"

    with JobStore(cfg.database_path) as store:
        baseline = store.request_counts()
        log(f"当日起始基线：{baseline}")
        log(f"参数：延迟 {cfg.detail_delay_seconds}、批 {cfg.detail_batch}、"
            f"冷却 {cfg.detail_cooldown_seconds}、日线 {cfg.max_requests_per_day}")

        # 素材：所有还缺 JD 的岗位，active 优先（探测顺便补真实数据）
        rows = store.conn.execute(
            "SELECT * FROM jobs WHERE trim(coalesce(job_description,'')) = '' "
            "AND raw_json != '{}' AND job_invalid = 0 "
            "ORDER BY CASE WHEN retention_status='active' THEN 0 ELSE 1 END, "
            "         canonical_id LIMIT ?",
            (args.max_details,),
        ).fetchall()
        log(f"候选 {len(rows)} 条待补详情")
        if not rows:
            log("无候选，退出")
            return

        collector = BossCollector(cfg)
        collector.ledger = store
        collector.ledger_script = "probe_ban_boundary"
        await collector.start()
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else SearchFilters()
        nav = collector._params(city, cfg.keywords[0], filters, 1)
        from urllib.parse import urlencode
        await collector._goto_search(
            f"{SEARCH_BASE}?{urlencode({k: v for k, v in nav.items() if k not in ('pageSize', 'scene')})}"
        )
        log(f"已导航：{collector.page.url[:70]}")

        result = {
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "baseline_requests_today": baseline.get("total", 0),
            "params": {
                "detail_delay_seconds": list(cfg.detail_delay_seconds),
                "detail_batch": cfg.detail_batch,
                "detail_cooldown_seconds": list(cfg.detail_cooldown_seconds),
                "burst_threshold": cfg.burst_threshold,
            },
            "banned": False,
        }
        done = failed = 0
        t0 = time.monotonic()
        try:
            for idx, row in enumerate(rows, start=1):
                job = _row_to_job(row)
                try:
                    job = await collector._collect_detail(job)
                except RiskControlStop:
                    raise
                except Exception as exc:
                    failed += 1
                    if "职位已不存在" in str(exc) or "已暂停" in str(exc):
                        job.job_invalid = True
                        store.upsert(job)
                    continue
                store.upsert(job)
                done += 1
                if done % 10 == 0:
                    today = store.request_counts().get("total", 0)
                    log(f"已补 {done} 条 | 今日累计请求 {today} | "
                        f"code37 {collector.code37_count} | "
                        f"{done / max(1e-9, (time.monotonic() - t0) / 3600):.0f} 条/小时")
                # 批间冷却（激进档）
                if done and done % cfg.detail_batch == 0:
                    lo, hi = cfg.detail_cooldown_seconds
                    from job_radar.throttle import gaussian
                    await asyncio.sleep(gaussian(lo, hi))
        except RiskControlStop as exc:
            # ★ 这就是要采的数据点
            today = store.request_counts()
            result.update(
                banned=True,
                stop_reason=str(exc),
                banned_after_details=done,
                requests_today_at_ban=today.get("total", 0),
                requests_this_probe=collector.request_count,
                code37_at_ban=collector.code37_count,
                breakdown_today=today,
                elapsed_minutes=round((time.monotonic() - t0) / 60, 1),
            )
            log("=" * 60)
            log(f"★ 命中风控：{exc}")
            log(f"★ 本轮补了 {done} 条详情后被拦")
            log(f"★ 当日累计请求 {today.get('total', 0)}"
                f"（探测开始时 {baseline.get('total', 0)}）")
            log(f"★ 本轮请求 {collector.request_count}，code=37 {collector.code37_count} 次")
            log("=" * 60)
        finally:
            if not result["banned"]:
                today = store.request_counts()
                result.update(
                    stop_reason="跑完候选或达 --max-details，未被封",
                    banned_after_details=done,
                    requests_today_at_ban=today.get("total", 0),
                    requests_this_probe=collector.request_count,
                    code37_at_ban=collector.code37_count,
                    breakdown_today=today,
                    elapsed_minutes=round((time.monotonic() - t0) / 60, 1),
                )
            result["failed"] = failed
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            await collector.close()

        log(f"成功 {done} / 失败 {failed}")
        log(f"结论已存：{out_path}")
        if not result["banned"]:
            log("未触发封禁——说明红线比本轮量级更高")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
