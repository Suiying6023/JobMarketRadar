"""批量检测岗位是否关闭（下架/失效），用隔离的隐身浏览器加载公开详情页。

与主采集账号完全隔离：
- 非持久上下文（无登录 cookie），不携带主账号 .browser-profile
- 只加载公开的 job_detail 页面，不调用 token 详情接口 → 不叠加主账号风控
- 识别"职位已关闭/已下线/已结束"等标记，命中则 job_invalid=1 + 降级为 sample

用法:
  python scripts/check_job_status.py --limit 50        # 检查前 50 条 active（未标记关闭的）
  python scripts/check_job_status.py --all             # 全部 active
  python scripts/check_job_status.py --delay 2.5       # 条间间隔（秒），默认 2.0-4.0 高斯
  # 走代理池避免 IP 风控（每 rotate-every 条换一个 IP）：
  python scripts/check_job_status.py --proxy-file proxies.txt --rotate-every 10
  # 单代理：
  python scripts/check_job_status.py --proxy http://user:pass@host:port
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
# 并行 mihomo 实例（独立于主 clash）：走 7899 代理 + 9098 API，节点轮换不影响主 clash
CLASH_PROXY = "http://127.0.0.1:7899"
CLASH_API = "http://127.0.0.1:9098"
CLASH_SECRET = "set-your-secret"

CLOSED_MARKERS = ("职位已关闭", "该职位已关闭", "已下线", "职位已下线", "职位已结束", "暂停招聘", "招聘已结束")

# 招聘者活跃度：库里出现过的全部文案（2026-08-27 现场统计，共 13 种非空值），
# 映射到"距今约多少天"用于判定长期不活跃。是有限枚举，所以精确匹配而不用模糊正则。
# ⚠️ 必须按长度降序匹配："本周活跃" 与 "2周内活跃" 都含"周活跃"，短的会误吃长的。
ACTIVE_DAYS = {
    "在线": 0, "刚刚活跃": 0, "今日活跃": 1, "日内活跃": 1,
    "3日内活跃": 3, "本周活跃": 7, "2周内活跃": 14,
    "本月活跃": 30, "月内活跃": 30, "2月内活跃": 60, "3月内活跃": 90,
    "4月内活跃": 120, "5月内活跃": 150, "近半年活跃": 180, "半年前活跃": 999,
}
# 按文案长度降序，保证先匹配更具体的
ACTIVE_PATTERNS = sorted(ACTIVE_DAYS.items(), key=lambda kv: -len(kv[0]))


def parse_active_status(text: str) -> tuple[str | None, int | None]:
    """从页面 innerText 里抽招聘者活跃度文案，返回 (文案, 约合天数)。

    只取第一个命中：页面上「更多职位」区块也会出现别人的活跃度，
    而当前岗位的那条排在最前（紧跟招聘者姓名）。
    """
    best = None
    for phrase, days in ACTIVE_PATTERNS:
        pos = text.find(phrase)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, phrase, days)
    if best is None:
        return None, None
    return best[1], best[2]


def _parse_proxy(s: str) -> dict:
    u = urlsplit(s.strip())
    p = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        p["username"] = u.username
        p["password"] = u.password or ""
    return p


def load_proxies(args) -> list[dict]:
    if args.proxy_file:
        lines = [l.strip() for l in Path(args.proxy_file).read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        return [_parse_proxy(l) for l in lines]
    if args.proxy:
        return [_parse_proxy(args.proxy)]
    return []


def _is_closed(text: str) -> bool:
    return any(m in text for m in CLOSED_MARKERS)


def _clash_api(path: str, method: str = "GET", body=None) -> dict | None:
    req = urllib.request.Request(f"{CLASH_API}{path}", method=method,
                                 headers={"Authorization": f"Bearer {CLASH_SECRET}"})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _clash_rotate(group: str) -> str | None:
    """并行实例组切到随机不同节点，返回新节点名（失败 None）。"""
    data = _clash_api("/proxies")
    if not data:
        return None
    g = data.get("proxies", {}).get(group)
    if not g:
        return None
    nodes = [n for n in g.get("all", []) if n != g.get("now")]
    if not nodes:
        return None
    target = random.choice(nodes)
    _clash_api(f"/proxies/{quote(group)}", "PUT", {"name": target})
    return target


def _clash_current(group: str) -> str | None:
    data = _clash_api("/proxies")
    if data:
        g = data.get("proxies", {}).get(group)
        if g:
            return g.get("now")
    return None


async def check_job(page, source_job_id: str, max_wait: float = 12.0) -> tuple[bool | None, str]:
    """加载详情页。返回 (是否关闭, 页面文本)。

    是否关闭：True=关闭，False=在招，None=未判定（挑战未过或页面空）。
    页面文本一并返回，调用方可从中解析活跃度，避免为此再请求一次。
    """
    url = f"https://www.zhipin.com/job_detail/{source_job_id}.html"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return None, ""
    # 等 BOSS 挑战页自动解析（标题不再是"请稍候"）
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < max_wait:
        try:
            title = await page.title()
        except Exception:
            return None, ""
        if "请稍候" not in title and "验证" not in title and title.strip():
            break
        await asyncio.sleep(1.5)
    # ⚠️ 标题恢复正常后 body 可能仍是空的（domcontentloaded 早于内容填充，
    # 实测同一批里第一条 len=0、第二条 1655）。这里再等 DOM 真的有内容。
    text = ""
    for _ in range(8):
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return None, ""
        if len(text) > 500:
            break
        await asyncio.sleep(0.8)
    if not text:
        return None, ""
    return _is_closed(text), text


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100, help="处理条数上限")
    parser.add_argument("--all", action="store_true", help="处理全部 active")
    parser.add_argument("--delay", type=float, default=0, help="条间固定间隔秒（0=2.0-4.0 高斯）")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--proxy", default="", help="单个代理 http://user:pass@host:port")
    parser.add_argument("--proxy-file", default="", help="代理池文件，每行一个")
    parser.add_argument("--rotate-every", type=int, default=10, help="代理每 N 条换一个 IP")
    parser.add_argument("--clash", action="store_true",
                        help="用并行 mihomo 节点轮换（走 127.0.0.1:7899，每 rotate-every 条切节点，结束还原；不影响主 clash）")
    parser.add_argument("--clash-group", default="🚀节点选择", help="轮换的 clash 代理组")
    parser.add_argument("--burst", type=int, default=25, help="每 N 条做一次长停顿（时间段性限速）")
    parser.add_argument("--burst-pause", type=float, default=0, help="批间长停顿秒（0=随机 45-120s）")
    parser.add_argument("--window", default="", help="运行时段 HH-HH（如 09-23），之外等待；默认不限")
    parser.add_argument("--storage-state", default="",
                        help="人工过验证后保存的 storage_state json（见 manual_verify_proxy.py）。"
                             "被标记的出口 IP 必须带它，否则每条都会撞「安全验证」页")
    parser.add_argument("--refresh-active", action="store_true",
                        help="同时刷新招聘者活跃度（recruiter_active_status），"
                             "并按 --stale-days 把长期不活跃的岗位降级")
    parser.add_argument("--stale-days", type=int, default=60,
                        help="配合 --refresh-active：活跃度换算天数 >= 此值即视为长期不活跃并降级为 sample")
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    sql = ("SELECT canonical_id, title, company_name, source_job_id FROM jobs "
           "WHERE retention_status='active' AND job_invalid=0 AND job_status_desc != '职位已关闭' "
           "ORDER BY canonical_id")
    rows = con.execute(sql).fetchall()
    if not args.all:
        rows = rows[: args.limit]
    print(f"待检测关闭状态: {len(rows)} 条")

    if not rows:
        return

    if args.window:
        try:
            w_lo, w_hi = map(int, args.window.split("-"))
        except ValueError:
            print("--window 格式应为 HH-HH，如 09-23"); return
        while not (w_lo <= datetime.now().hour < w_hi):
            print(f"[节奏] 当前 {datetime.now().hour} 点，不在时段 {args.window}，等待...", flush=True)
            await asyncio.sleep(300)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=args.headless,
            args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        proxies = load_proxies(args)
        current_proxy = None
        ctx_kw = {"viewport": {"width": 1366, "height": 768}}
        # 被 BOSS 标记的出口 IP 会把每条请求都送去「安全验证」页，那种页面不会自解。
        # 带上人工过验证后存的 cookie 才能继续读（manual_verify_proxy.py 产出）。
        if args.storage_state:
            sp = Path(args.storage_state)
            if not sp.exists():
                print(f"--storage-state 文件不存在: {sp}"); return
            # 对称守卫：这条链路必须免登录。带账号 cookie 走跳板 IP =
            # 用陌生 IP 操作主账号，是比 IP 被标记严重得多的风控风险。
            try:
                blob = json.loads(sp.read_text(encoding="utf-8"))
                names = {c.get("name") for ck in blob.get("cookies", []) for c in (ck,)}
                bad = sorted(names & {"wt2", "wt", "geek_zp_token", "zp_at", "bst"})
                if bad:
                    print(f"拒绝启动：{sp} 含账号登录态 cookie {bad}。")
                    print("波尔出口必须免登录，请重新生成一份只过验证、不登录的 state。")
                    return
            except (OSError, json.JSONDecodeError) as exc:
                print(f"--storage-state 无法解析: {exc}"); return
            ctx_kw["storage_state"] = str(sp)
            print(f"[验证] 复用人工验证态: {sp}（已确认无登录 cookie）", flush=True)
        orig_node = None
        if args.clash:
            ctx_kw["proxy"] = {"server": CLASH_PROXY}
            orig_node = _clash_current(args.clash_group)
            print(f"[clash] 并行实例组 {args.clash_group} 当前: {orig_node}", flush=True)
        elif proxies:
            current_proxy = proxies[0]
            ctx_kw["proxy"] = current_proxy
        ctx = await browser.new_context(**ctx_kw)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        closed = 0
        unknown = 0
        stale = 0
        refreshed = 0
        for idx, r in enumerate(rows, 1):
            if args.clash:
                if idx > 1 and (idx - 1) % args.rotate_every == 0:
                    new = _clash_rotate(args.clash_group)
                    print(f"[clash] 节点 -> {new}", flush=True)
                    await asyncio.sleep(2.0)
            elif proxies and len(proxies) > 1:
                p = proxies[((idx - 1) // args.rotate_every) % len(proxies)]
                if p != current_proxy:
                    await ctx.close()
                    rebuild = {"viewport": {"width": 1366, "height": 768}, "proxy": p}
                    if args.storage_state:
                        rebuild["storage_state"] = args.storage_state
                    ctx = await browser.new_context(**rebuild)
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    current_proxy = p
            result, page_text = await check_job(page, r["source_job_id"])
            if result is True:
                con.execute(
                    "UPDATE jobs SET job_invalid=1, job_status_desc='职位已关闭', retention_status='sample' "
                    "WHERE canonical_id=? AND retention_status='active'",
                    (r["canonical_id"],))
                con.commit()
                closed += 1
                print(f"[关闭] {r['title'][:26]} | {r['company_name']}", flush=True)
            elif result is False:
                # 在招。可选：顺带刷新活跃度，并把长期不活跃的降级
                if args.refresh_active and page_text:
                    phrase, days = parse_active_status(page_text)
                    if phrase:
                        con.execute(
                            "UPDATE jobs SET recruiter_active_status=?, recruiter_online=?, "
                            "       updated_at=? WHERE canonical_id=?",
                            (phrase, 1 if phrase == "在线" else 0,
                             datetime.now().isoformat(timespec="seconds"), r["canonical_id"]))
                        refreshed += 1
                        if days is not None and days >= args.stale_days:
                            # 降级不等于关闭：岗位还在招，只是招聘者久未上线，
                            # 投递价值低。留在库里做增量去重，不写 job_invalid。
                            con.execute(
                                "UPDATE jobs SET retention_status='sample' "
                                "WHERE canonical_id=? AND retention_status='active' AND bookmarked=0",
                                (r["canonical_id"],))
                            stale += 1
                            print(f"[不活跃-{phrase}] {r['title'][:24]} | {r['company_name']}", flush=True)
                        con.commit()
            else:
                unknown += 1
                print(f"[未判定] {r['title'][:26]} | {r['company_name']}", flush=True)
            if idx % 10 == 0 or idx == len(rows):
                extra = f"，活跃度更新 {refreshed}，长期不活跃降级 {stale}" if args.refresh_active else ""
                print(f"进度 {idx}/{len(rows)}，关闭 {closed}，未判定 {unknown}{extra}", flush=True)
            if idx < len(rows):
                if args.burst and idx % args.burst == 0:
                    bp = args.burst_pause or random.uniform(45.0, 120.0)
                    print(f"[节奏] 长停顿 {bp:.0f}s（第{idx}条后）", flush=True)
                    await asyncio.sleep(bp)
                else:
                    d = args.delay or random.uniform(5.0, 10.0)
                    await asyncio.sleep(d)
        if args.clash and orig_node:
            _clash_api(f"/proxies/{quote(args.clash_group)}", "PUT", {"name": orig_node})
            print(f"[clash] 已还原节点: {orig_node}", flush=True)
        await browser.close()

    tail = f"，活跃度更新 {refreshed}，长期不活跃降级 {stale}" if args.refresh_active else ""
    print(f"\n完成: 检测 {len(rows)}，关闭 {closed}，未判定 {unknown}{tail}")
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
