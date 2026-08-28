"""边界探测：激进参数下试探封禁红线

探测目标：
- code37_limit 从 35 提到 60（旧值按裸配额标的，新 header 后窗口可能更宽）
- detail_delay 从 [4,6] 降到 [2,4]（加快消耗速度）
- 监控 dump 目录，出现 code=32/36 立即停止

安全保障：
- 日线预算仍是 600（硬上限）
- 实时监控风控 dump
- 单轮限 100 条（分批试探）
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# 项目根
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from job_radar.collectors.boss import BossCollector
from job_radar.models import CollectionConfig
from job_radar.storage import JobStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RISK_DUMP_DIR = Path(__file__).parent.parent / "data" / "risk_dumps"


def check_hard_ban() -> tuple[bool, str]:
    """检查是否出现真封禁（code=32/36）"""
    if not RISK_DUMP_DIR.exists():
        return False, ""

    dumps = sorted(RISK_DUMP_DIR.glob("*.txt"))
    if not dumps:
        return False, ""

    # 只看最新 5 个
    for f in dumps[-5:]:
        try:
            body = json.loads(f.read_text(encoding="utf-8"))
            code = body.get("code")
            if code in (32, 36):
                return True, f"code={code} in {f.name}"
        except Exception:
            pass
    return False, ""


async def boundary_probe(limit: int = 100):
    """边界探测主逻辑"""
    storage = JobStorage("data/hangzhou_agent.sqlite3")

    # 激进配置
    config = CollectionConfig(
        collect_details=True,
        max_requests_per_run=limit,
        code37_limit=60,  # 从 35 提到 60
        detail_delay_min=2.0,  # 从 4 降到 2
        detail_delay_max=4.0,  # 从 6 降到 4
        burst_threshold=8,
        burst_penalty_seconds=15.0,
        detail_batch=6,
        detail_cooldown_seconds=25.0,
    )

    # 获取 active 无 JD 的记录（优先级最高）
    rows = list(storage.conn.execute(
        """SELECT canonical_id, source_job_id, job_url, raw_json
           FROM jobs
           WHERE retention_status = 'active'
             AND job_invalid = 0
             AND (job_description IS NULL OR job_description = '')
             AND raw_json != '{}'
           ORDER BY last_seen_at DESC
           LIMIT ?""",
        (limit,)
    ))

    if not rows:
        log.info("无待补记录")
        return

    log.info(f"准备补 {len(rows)} 条 active 无 JD 记录（激进参数）")
    log.info(f"  code37_limit={config.code37_limit}  detail_delay=[{config.detail_delay_min},{config.detail_delay_max}]")

    # 转成 JobRecord（简化版，只为拿 securityId）
    from job_radar.models import JobRecord
    jobs = []
    for can_id, src_id, url, raw_str in rows:
        try:
            raw = json.loads(raw_str)
            # 最小化构造
            job = JobRecord(
                platform="boss",
                source_job_id=src_id or can_id.split(":", 1)[-1],
                job_url=url,
                raw=raw,
            )
            jobs.append(job)
        except Exception as e:
            log.warning(f"跳过 {can_id}: {e}")

    if not jobs:
        log.info("无有效记录")
        return

    collector = BossCollector(storage, config)

    try:
        success = 0
        fail = 0

        for i, job in enumerate(jobs, 1):
            # 每 10 条检查一次封禁
            if i % 10 == 0:
                banned, reason = check_hard_ban()
                if banned:
                    log.error(f"!!! 检测到真封禁: {reason}，立即停止")
                    break

            try:
                updated = await collector._collect_detail(job)
                storage.upsert(updated)
                success += 1
                log.info(f"  [{i}/{len(jobs)}] {job.title or job.source_job_id} ✓")
            except Exception as e:
                fail += 1
                log.warning(f"  [{i}/{len(jobs)}] {job.source_job_id} ✗ {e}")

            # 检查 code37 是否超阈值
            if collector.code37_count >= config.code37_limit:
                log.warning(f"code37 达到 {collector.code37_count}，触发熔断")
                break

        log.info(f"探测完成: 成功 {success}  失败 {fail}  code37={collector.code37_count}")

        # 最终检查
        banned, reason = check_hard_ban()
        if banned:
            log.error(f"!!! 封禁: {reason}")
        else:
            log.info("✓ 零封禁")

    finally:
        await collector.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="边界探测")
    parser.add_argument("--limit", type=int, default=100, help="单轮条数（默认 100）")
    args = parser.parse_args()

    asyncio.run(boundary_probe(args.limit))
