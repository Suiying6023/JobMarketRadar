"""长期监督采集：一条命令跑完"列表扩充 → 补详情 → 标签提取 → 导出"，并自动停在安全线内。

**为什么需要它**：现在扩大量级要手工串 4-5 个脚本，每个都得先查余量、跑完再看结果。
而真正的约束是**跨进程的单日请求预算**（`request_log` 账本），手工串很容易在第三个
脚本才发现额度已经用完，或者反过来保守到只用了一半额度。

**设计原则**：
1. **每个阶段前查账本**，剩余额度不够就跳过该阶段（不是硬失败）
2. **预留额度**：默认留 15% 余量给突发/明天早上，不把日线跑满
3. **可中断可续跑**：所有状态在 DB 里，Ctrl+C 或断电后重跑就是续跑，不需要 resume 文件
4. **顺序按"每次请求的价值"排**：列表最便宜且产出新岗位 → active 补详情（投递池
   直接受益）→ sample 补详情（只为样本，最后做）
5. **零请求阶段不受预算约束**：LLM 标签提取、工商富化、导出都不碰 BOSS，随时可跑

用法：
    python scripts/daily_cycle.py                  # 完整一轮
    python scripts/daily_cycle.py --dry-run        # 只看计划，不发请求
    python scripts/daily_cycle.py --reserve 0.3    # 留 30% 额度
    python scripts/daily_cycle.py --skip list      # 跳过某阶段
    python scripts/daily_cycle.py --pages 10       # 列表翻更深（挖被截断的宽泛词）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

REQ_PER_DETAIL = JobStore.REQ_PER_DETAIL  # 真源在 storage，别另写一份


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(script: str, *args: str) -> int:
    """跑一个子脚本，**实时转发它的输出**。

    用 uv run 保证依赖（yaml/patchright）可用。
    必须逐行转发而不是让子进程直接继承 stdout：重定向到文件时（nohup / 后台跑）
    子进程的 stdout 变成全缓冲，几十分钟的采集日志会一直攒在缓冲区里不落盘，
    外部看上去像卡死了——长期监督跑必须能实时看到进度。
    PYTHONUNBUFFERED 让子进程自己也别缓冲。
    """
    cmd = ["uv", "run", "python", str(ROOT / "scripts" / script), *args]
    log(f"$ {script} {' '.join(args)}")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        # uv 每次调用都打一堆 warning/Installed 噪声，过滤掉
        if line.startswith("warning:") or line.startswith(("Uninstalled", "Installed")):
            continue
        if line:
            print(f"    │ {line}", flush=True)
    return proc.wait()


def budget_left(cfg, reserve: float) -> tuple[int, int, int]:
    """可用额度 = 上限 × (1 - 预留比) − 今日已用。返回 (剩余, 已用, 可用上限)。"""
    with JobStore(cfg.database_path) as store:
        used = store.request_counts().get("total", 0)
    usable = int(cfg.max_requests_per_day * (1 - reserve))
    return max(0, usable - used), used, usable


def new_jobs_today(cfg) -> int:
    """今天新入库的岗位数。

    ⚠️ `first_seen_at` 存的是 **UTC**（`...T18:39:15Z`），而 `request_log.day` 是
    本地日。拿本地日期去比 UTC 字段永远差 8 小时——北京时间凌晨 0-8 点期间
    UTC 还在前一天，统计恒为 0（2026-08-11 02:xx 实测踩到）。
    这里统一转成 UTC 日再比。
    """
    from datetime import timezone
    utc_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with JobStore(cfg.database_path) as store:
        return store.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE substr(first_seen_at,1,10) = ?",
            (utc_day,),
        ).fetchone()[0]


def pending_counts(cfg) -> dict[str, int]:
    """各阶段待处理量。判"有没有值"一律用 trim 谓词——那些列是
    NOT NULL DEFAULT ''，用 is not null 会恒真返回全表。"""
    with JobStore(cfg.database_path) as store:
        q = lambda s: store.conn.execute(s).fetchone()[0]
        return {
            "active_no_jd": q(
                "SELECT COUNT(*) FROM jobs WHERE retention_status='active' "
                "AND trim(coalesce(job_description,''))='' AND job_invalid=0 "
                "AND raw_json != '{}'"),
            "sample_no_jd": q(
                "SELECT COUNT(*) FROM jobs WHERE retention_status='sample' "
                "AND trim(coalesce(job_description,''))='' AND job_invalid=0 "
                "AND raw_json != '{}'"),
            # ⚠️ JSON 列的"空"有两种写法：'' 和 '{}'。只判 '' 会把 63 条
            # 待提取的岗位当成已覆盖，静默跳过整个阶段（2026-08-11 实测踩到）。
            # 标签用 glm_tags_updated_at 判（和 extract_glm_tags.py:148 同一口径，
            # 时间戳列不受 '{}' 影响）——两处口径必须一致，否则会像
            # backfill_resume 那次一样"这边说有 231 条待处理、那边选到 0 条"空转。
            "no_tags": q(
                "SELECT COUNT(*) FROM jobs WHERE retention_status='active' "
                "AND job_description != '' AND glm_tags_updated_at = ''"),
            # 按 brand_id 去重数（和 enrich_company_info.py:86 同口径）——
            # 富化是按公司做的，一次请求更新该公司全部岗位。数岗位数会虚高
            # （52 个岗位其实只有 42 家公司）。
            "no_company_reg": q(
                "SELECT COUNT(*) FROM ("
                "  SELECT brand_id, MAX(CASE WHEN company_reg_json != '{}' "
                "         THEN 1 ELSE 0 END) done"
                "  FROM jobs WHERE retention_status='active' AND brand_id != ''"
                "  GROUP BY brand_id) WHERE done = 0"),
        }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.hangzhou.yaml")
    p.add_argument("--reserve", type=float, default=0.15,
                   help="预留额度比例，默认 0.15（不把日线跑满）")
    p.add_argument("--pages", type=int, default=0,
                   help="列表每词页数。0（默认）= 按配置的 pages_per_keyword "
                        "/ pages_per_query 分词决定；给数值则统一覆盖")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip", action="append", default=[],
                   choices=["list", "active", "sample", "tags", "company", "export"],
                   help="跳过某阶段，可重复")
    args = p.parse_args()

    cfg = load_config(args.config)
    t0 = time.monotonic()

    left, used, usable = budget_left(cfg, args.reserve)
    pend = pending_counts(cfg)

    log("=" * 60)
    log(f"今日已用 {used} / 可用 {usable}"
        f"（上限 {cfg.max_requests_per_day}，预留 {args.reserve:.0%}）→ 剩余 {left}")
    log(f"待处理：active无JD {pend['active_no_jd']} | sample无JD {pend['sample_no_jd']} "
        f"| 无标签 {pend['no_tags']} | 无工商 {pend['no_company_reg']}")
    log("=" * 60)

    if args.dry_run:
        log("--dry-run：以下是计划，不发任何请求")

    # ---- 阶段 1：列表扩充（最便宜，产出新岗位）----
    if "list" not in args.skip:
        # 预估按每个词各自的深度求和（配了 pages_per_keyword 时各词不同），
        # 乘城市数——expand_list 遍历全部城市，只算一城会低估数倍。
        per_kw = [args.pages or cfg.pages_for(kw) for kw in cfg.keywords]
        est = sum(per_kw) * len(cfg.cities)
        if left < est * 0.4:
            log(f"⏭️  跳过列表：预估 {est} 次，剩余 {left} 不足")
        else:
            depth = (f"统一 {args.pages} 页" if args.pages
                     else f"按词 {min(per_kw)}-{max(per_kw)} 页")
            log(f"▶ 阶段1 列表扩充（{len(cfg.keywords)} 词 × {len(cfg.cities)} 城，"
                f"{depth}，预估 ≤{est} 次；深翻靠 --dry-stop 提前收尾）")
            if not args.dry_run:
                before_new = new_jobs_today(cfg)
                # --pages 0 = 让 expand_list 自己按词读配置
                run("expand_list.py", "--config", args.config,
                    "--pages", str(args.pages))
                left, used, usable = budget_left(cfg, args.reserve)
                pend = pending_counts(cfg)
                gained = new_jobs_today(cfg) - before_new
                log(f"  ↳ 本阶段新增 {gained} 个岗位，剩余额度 {left}，"
                    f"待补详情 active {pend['active_no_jd']}")

    # ---- 阶段 2：active 补详情（投递池直接受益，优先）----
    if "active" not in args.skip and pend["active_no_jd"]:
        can = int(left / REQ_PER_DETAIL)
        n = min(pend["active_no_jd"], can)
        if n <= 0:
            log(f"⏭️  跳过 active 详情：额度不足（剩余 {left}）")
        else:
            log(f"▶ 阶段2 active 补详情 {n} 条（预估 {int(n * REQ_PER_DETAIL)} 次）")
            if not args.dry_run:
                run("backfill_details.py", "--config", args.config, "--limit", str(n))
                left, used, usable = budget_left(cfg, args.reserve)
                log(f"  ↳ 剩余额度 {left}")

    # ---- 阶段 3：sample 补详情（只为样本量，放最后）----
    if "sample" not in args.skip and pend["sample_no_jd"]:
        can = int(left / REQ_PER_DETAIL)
        n = min(pend["sample_no_jd"], can)
        if n <= 0:
            log(f"⏭️  跳过 sample 详情：额度不足（剩余 {left}）")
        else:
            log(f"▶ 阶段3 sample 补详情 {n} 条（预估 {int(n * REQ_PER_DETAIL)} 次）")
            if not args.dry_run:
                run("backfill_details.py", "--config", args.config,
                    "--limit", str(n), "--include-sample")
                left, used, usable = budget_left(cfg, args.reserve)
                log(f"  ↳ 剩余额度 {left}")

    # ---- 以下阶段零 BOSS 请求，不受预算约束 ----
    if "tags" not in args.skip:
        pend = pending_counts(cfg)
        if pend["no_tags"]:
            log(f"▶ 阶段4 LLM 标签提取 {pend['no_tags']} 条（不碰 BOSS，走外部 LLM 网关）")
            if not args.dry_run:
                # 这个脚本用 --db 而不是 --config，模型/网关读 .env 的 LLM_*
                run("extract_glm_tags.py", "--db", cfg.database_path)
        else:
            log("✓ 标签已全覆盖")

    if "company" not in args.skip:
        pend = pending_counts(cfg)
        if pend["no_company_reg"]:
            log(f"▶ 阶段5 工商富化 {pend['no_company_reg']} 家"
                f"（免登录 gongsi 页，独立 profile 不碰采集账号）")
            if not args.dry_run:
                run("enrich_company_info.py", "--config", args.config)
        else:
            log("✓ 工商信息已全覆盖")

    if "export" not in args.skip:
        log("▶ 阶段6 导出投递池 HTML")
        if not args.dry_run:
            run("export_pool_html.py")

    left, used, usable = budget_left(cfg, args.reserve)
    mins = (time.monotonic() - t0) / 60
    log("=" * 60)
    log(f"一轮完成，耗时 {mins:.0f} 分钟。今日已用 {used}/{usable}（剩 {left}）")
    log("查看投递池: python scripts/serve_pool.py")


if __name__ == "__main__":
    from _online_guard import guard
    guard(allow_dry_run=True)
    main()
