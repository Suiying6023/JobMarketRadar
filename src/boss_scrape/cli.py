"""命令行入口：`python -m boss_scrape ...`

只做三件事：
  probe   —— 查账号此刻状态（1 个请求，不重试）
  search  —— 按关键词翻页，产出岗位；可选连详情一起抓
  verify  —— 打开窗口，若当前是验证页就等人过完

输出一律 JSON/JSONL，方便管道到别处，不写数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .client import BossClient
from .errors import (AccountRestricted, BossScrapeError, LoginExpired,
                     RiskControl, Throttled, VerificationRequired)

DEFAULT_PROFILE = ".browser-profile"


def _client(args) -> BossClient:
    return BossClient(profile=args.profile, headless=args.headless,
                      delay=tuple(args.delay), burst_every=args.burst_every,
                      block_heavy=not args.no_block, log_path=args.log,
                      seed_path=args.state)


async def cmd_probe(args) -> int:
    async with _client(args) as client:
        user = await client.user_id()
        print(json.dumps({"logged_in": bool(user), "user_id": user,
                          "stats": client.stats.as_dict()}, ensure_ascii=False))
    return 0


async def cmd_search(args) -> int:
    records: list[dict] = []
    details_ok = 0
    async with _client(args) as client:
        async for card in client.search(args.query, city=args.city,
                                        pages=args.pages, page_size=args.page_size,
                                        filters=dict(f.split("=", 1) for f in args.filter)):
            if args.limit and len(records) >= args.limit:
                break
            record = _card_dict(card)
            if args.details and details_ok < (args.details or 0):
                try:
                    detail = await client.detail(card)
                    record["detail"] = {
                        "description": detail.description,
                        "address": detail.address,
                        "job_status": detail.job_status,
                        "recruiter_id": detail.recruiter_id,
                        "recruiter_active": detail.recruiter_active,
                        "recruiter_active_days": detail.recruiter_active_days,
                        "is_proxy": detail.is_proxy,
                        "invalid": detail.invalid,
                        "company_intro": detail.company_intro,
                        "company_founded_year": detail.company_founded_year,
                        "skills": detail.skills,
                    }
                    details_ok += 1
                except BossScrapeError as exc:
                    record["detail_error"] = f"{type(exc).__name__}: {exc}"[:200]
            records.append(record)
            print(f"[{len(records):03d}] {card.title} | {card.company} | "
                  f"{card.salary_text} | {card.location_text}", file=sys.stderr)

    _emit(records, args.out)
    print(json.dumps({"jobs": len(records), "details": details_ok,
                      "stats": client.stats.as_dict()}, ensure_ascii=False))
    return 0


def cmd_logs(args) -> int:
    """把日志压成可判断的结论：风控事件之前那段时间发了多少请求。"""
    from .runlog import summarize

    try:
        summary = summarize(args.path, window_s=args.window)
    except FileNotFoundError:
        print(json.dumps({"error": "no_log", "path": args.path}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


async def cmd_verify(args) -> int:
    async with _client(args) as client:
        try:
            await client.bootstrap()
        except VerificationRequired:
            await client.show_window()
            print(json.dumps({"needs_verification": True,
                              "message": "已在浏览器窗口打开验证页，请完成验证。"},
                             ensure_ascii=False))
            for _ in range(args.seconds):
                await asyncio.sleep(1)
                html = await client._page_html()
                url = client.page.url if client.page else ""
                if "verify" not in url and "安全验证" not in html:
                    print(json.dumps({"needs_verification": False,
                                      "message": "验证已通过。"}, ensure_ascii=False))
                    return 0
            print(json.dumps({"needs_verification": True,
                              "message": "等待超时，验证仍未完成。"}, ensure_ascii=False))
            return 1
        print(json.dumps({"needs_verification": False,
                          "message": "当前没有要求人工验证。"}, ensure_ascii=False))
    return 0


def _card_dict(card) -> dict:
    return {
        "job_id": card.job_id, "title": card.title, "company": card.company,
        "url": card.url, "brand_id": card.brand_id,
        "city": card.city, "district": card.district,
        "business_district": card.business_district,
        "salary_text": card.salary_text, "salary_min_k": card.salary_min_k,
        "salary_max_k": card.salary_max_k, "salary_months": card.salary_months,
        "daily_rate": card.daily_rate,
        "experience": card.experience, "education": card.education,
        "company_size": card.company_size, "financing_stage": card.financing_stage,
        "industry": card.industry,
        "recruiter_name": card.recruiter_name, "recruiter_title": card.recruiter_title,
        "recruiter_active": card.recruiter_active, "recruiter_online": card.recruiter_online,
        "skills": card.skills, "labels": card.labels, "welfare": card.welfare,
        "job_type": card.job_type, "security_id": card.security_id,
        "keyword": card.keyword, "rank": card.rank,
    }


def _emit(records: list[dict], out: str) -> None:
    if not out:
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boss_scrape",
                                     description="BOSS 直聘抓取层（不含存储与展示）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="浏览器用户目录")
    parser.add_argument("--headless", action="store_true", help="无头模式（过验证时不可用）")
    parser.add_argument("--delay", type=float, nargs=2, default=(4.0, 8.0),
                        metavar=("LOW", "HIGH"), help="每请求间隔秒数区间")
    parser.add_argument("--burst-every", type=int, default=25, help="每多少个请求插入一次长停顿")
    parser.add_argument("--no-block", action="store_true", help="不拦 image/font/media")
    parser.add_argument("--log", default="logs/scrape.jsonl",
                        help="运行日志（JSONL）；每次真实运行都会追加，用来标定风控阈值")
    parser.add_argument("--state", default="data/seed-state.json",
                        help="种子落盘位置；有它就能跳过整页加载，空串表示不持久化")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="查账号状态（1 个请求）")
    probe.set_defaults(func=cmd_probe)

    search = sub.add_parser("search", help="按关键词抓岗位")
    search.add_argument("--query", required=True)
    search.add_argument("--city", default="101210100", help="城市码，杭州=101210100")
    search.add_argument("--pages", type=int, default=1)
    search.add_argument("--page-size", type=int, default=30)
    search.add_argument("--limit", type=int, default=0, help="最多产出多少条，0=不限")
    search.add_argument("--filter", action="append", default=[], metavar="K=V",
                        help="平台筛选码，可重复，如 --filter jobType=1901")
    search.add_argument("--details", type=int, default=0, help="顺带抓多少条详情")
    search.add_argument("--out", default="", help="写入 JSON 文件；留空则逐行输出")
    search.set_defaults(func=cmd_search)

    verify = sub.add_parser("verify", help="检查并等待人工验证")
    verify.add_argument("--seconds", type=int, default=600)
    verify.set_defaults(func=cmd_verify)

    logs = sub.add_parser("logs", help="汇总运行日志，反推风控阈值")
    logs.add_argument("--path", default="logs/scrape.jsonl")
    logs.add_argument("--window", type=float, default=60.0,
                      help="统计风控事件前多少秒内的请求数")
    logs.set_defaults(func=cmd_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `logs` 是纯本地统计，不需要事件循环；其余子命令是协程。
    if not asyncio.iscoroutinefunction(args.func):
        return args.func(args)
    try:
        return asyncio.run(args.func(args))
    except VerificationRequired as exc:
        print(json.dumps({"error": "verification_required", "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 3
    except AccountRestricted as exc:
        print(json.dumps({"error": "account_restricted", "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 4
    except LoginExpired as exc:
        print(json.dumps({"error": "login_expired", "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 5
    except Throttled as exc:
        print(json.dumps({"error": "throttled", "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 6
    except RiskControl as exc:
        print(json.dumps({"error": "risk_control", "message": str(exc),
                          "code": exc.code}, ensure_ascii=False), file=sys.stderr)
        return 7
    except BossScrapeError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 8


if __name__ == "__main__":
    sys.exit(main())
