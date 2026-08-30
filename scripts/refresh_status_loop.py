"""状态刷新的带人工回环版：出口 IP 被标记时自动弹验证窗口，人过完继续跑。

为什么要它：`check_job_status.py` 遇到「安全验证」页只能记未判定并继续，
于是一旦出口被标记，后面几百条会全部空转（实测两轮都在 150 条左右开始连续未判定）。
本脚本把「检测 → 发现被标记 → 弹窗人工过 → 继续」做成闭环。

判据（都来自实测，不要改成别的）：
  · 出口是否可用 = **用浏览器真读一条 job_detail**（probe_detail）。
    ⚠️ 不要用首页返回码：实测首页 200 而详情页全是「安全验证」，它是假绿灯，
    只够回答隧道通不通。裸 curl 打详情页也不行——正常挑战同样是 302。
  · 人工过验证 ≠ 出口恢复：实测验证通过后详情页仍可能是「安全验证」，
    所以验证后必须再 probe_detail 复检，不能复检首页码就放行。
  · 必须有头模式。无头下 BOSS 的 JS 挑战页不自解，表现是每条都「未判定」。
  · 连续未判定 >= FAIL_STREAK 条即认为出口失效，立刻停下检查，不要傻跑完。

用法：
    ssh -f -N -D 1082 -o ServerAliveInterval=30 bohrium-container
    ssh -f -N -D 1080 -o ServerAliveInterval=30 bohrium-vm
    uv run python scripts/refresh_status_loop.py --batch 120

⚠️ **成本认知**：整页加载一条 = **13 次以上 HTTP 请求**（实测，含 geetest 与
   `logapi.zhipin.com` 埋点）。而老的 JSON API 链路一条只有 1 次。
   所以"条间 5-10s"看着温和，真实速率约 14-26 请求/分钟——
   而 08-11 那次跑 1025 次未触发封禁的运行是均值 6.7、峰值 12.5 请求/分钟。
   **估算风险要按请求数，不能按条数**，这是 08-27 被封的根因。
   （另注：08-11 那轮是 `probe_ban_boundary.py` 的激进档探测，上界未知，
   不是安全线；别拿它当"1000 次没问题"的依据。）

⚠️ 起之前必须确认没有另一个本脚本在跑（2026-08-27 踩过）：
   两个进程各起浏览器、共用同两个出口 = 实际速率翻倍，十来条就把两个 IP 都打成
   「安全验证」，而且会各弹一个验证窗口，人过了一个另一个还在傻等。
   查：Get-CimInstance Win32_Process -Filter "Name='python.exe'" 看 CommandLine。

⚠️ 全程免登录。绝不要在弹出的验证窗口里登录 BOSS 账号——
   带账号 cookie 走跳板 IP 的风控风险远大于收益。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"

# 一条岗位页实际发出多少次 HTTP 请求。实测 verify 页就 13 个（www.zhipin.com 4、
# static.geetest.com 3、api.geetest.com 2、static.zhipin.com 2、img 1、logapi 1），
# 成功的岗位页更多，取 14 作保守估计。
# 存在理由：老的 JSON API 链路一条 = 1 次请求，账本直接数条数就行；
# 这条链路一条 = 十几次，**按条数记账会把风险低估一个数量级**（08-27 就是这样被封的）。
REQ_PER_JOB = 14

sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import (  # noqa: E402
    CLOSED_MARKERS, ACTIVE_DAYS, parse_active_status, _is_closed,
)

# 出口池：(标识, SOCKS 端口)
EXITS = [("vm", 1080), ("container", 1082)]
FAIL_STREAK = 10         # 连续这么多条未判定才怀疑出口失效（先探再判，别急着弹窗）
PROBE_URL = "https://www.zhipin.com/"

# ⚠️ 首页返回码是**假绿灯**，不要用它判「能不能读岗位页」（2026-08-27 实测推翻）。
#    实测同一时刻：首页 200，而 job_detail 每一条都落到「安全验证」页（len=101）。
#    连续 6 次、间隔 20s 也全部如此，说明不是配额窗口而是详情页级别的标记。
#    首页几乎不设防，被标记的 IP 照样 200 —— 所以它只能回答「隧道通不通」。
#    唯一可靠判据是**用浏览器真的读一条 job_detail**，见 probe_detail()。
#    裸 curl 打 job_detail 也不行：未被标记的 IP 同样会 302 到 security.html
#    （那个 302 就是 JS 挑战本身，浏览器会跟过去解掉再回来），
#    所以裸 curl 无法区分「正常挑战」和「已被标记」。


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INFO_URL = "https://www.zhipin.com/wapi/zpsecureflow/forbidden-info?"


def exit_status(port: int, timeout: int = 20) -> tuple[str, str]:
    """查出口封禁状态，一条 curl，不起浏览器。返回 (verdict, 说明)。

    verdict: ok / verify（可人工救）/ blocked（硬封只能等）/ dead / unknown

    判据来自 BOSS 自己的接口，比"起浏览器读一条详情页"快一个数量级
    （90s → 2s），而且直接给出精确解封时间和真实出口 IP。
    """
    try:
        out = subprocess.run(
            ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
             "--max-time", str(timeout), "-H", f"User-Agent: {UA}",
             "-H", "X-Requested-With: XMLHttpRequest", INFO_URL],
            capture_output=True, text=True, timeout=timeout + 6)
        raw = (out.stdout or "").strip()
    except Exception:
        return "dead", "隧道不通"
    if not raw:
        return "dead", "隧道无响应"
    try:
        zp = (json.loads(raw) or {}).get("zpData")
    except json.JSONDecodeError:
        return "unknown", f"非 JSON: {raw[:60]}"
    if not zp or zp.get("code") != 31:
        return "ok", "无限制记录"
    exp = zp.get("expireTime")
    when = ""
    if isinstance(exp, (int, float)):
        d = datetime.fromtimestamp(exp / 1000)
        left = (d - datetime.now()).total_seconds() / 3600
        when = f"解封 {d:%m-%d %H:%M}（还剩 {left:.1f}h）"
    ip = zp.get("ip") or "?"
    if zp.get("jumpVerify"):
        return "verify", f"IP {ip} 被标记，可人工验证。{when}"
    return "blocked", f"IP {ip} 硬封，验证无效。{when}"


def log_requests(con: sqlite3.Connection, n: int, outcome: str = "ok",
                 code: int | None = None) -> None:
    """把一条岗位页折算成 n 次请求记进 request_log，kind 固定 `detail_page`。

    共用那张表只为「一个地方看全部请求」，**不共用日线**：这条链路免登录、
    走波尔机房 IP，与采集链路（带账号 cookie、家宽直连）的风控主体完全不同。
    两侧各按自己的 kind 记账 —— 见 `used_today()` 与
    `JobStore.request_counts()` 的 `COLLECT_KINDS`。
    """
    now = datetime.now().astimezone()
    con.executemany(
        "INSERT INTO request_log(ts, day, kind, outcome, code, script, note) "
        "VALUES (?,?,?,?,?,?,?)",
        [(now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
          "detail_page", outcome, code, "refresh_status_loop", "")] * n)
    con.commit()


def used_today(con: sqlite3.Connection) -> int:
    """今日**本链路**已用请求数，只数 `detail_page`。

    原先数的是全表，于是采集链路当天跑过的量会顶掉刷新链路的额度（反之亦然），
    两条本该独立的安全线互相扣血。风控按 IP + 账号判，两条链路既不同 IP
    也不同登录态，没有理由共享同一条线。
    """
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    return con.execute(
        "SELECT count(*) FROM request_log WHERE day=? AND kind='detail_page'",
        (day,)).fetchone()[0]


def probe_exit(port: int, timeout: int = 12) -> int:
    """返回 BOSS 首页 HTTP 码。**只用来判隧道通不通**：0=不通，非 0=通。

    不要拿 200 当「出口可用」——见上方注释，实测 200 也可能读不了岗位页。
    """
    try:
        out = subprocess.run(
            ["curl", "-sS", "--socks5-hostname", f"127.0.0.1:{port}",
             "--max-time", str(timeout), "-o", "/dev/null",
             "-w", "%{http_code}", PROBE_URL],
            capture_output=True, text=True, timeout=timeout + 5)
        return int((out.stdout or "0").strip()[:3] or 0)
    except Exception:
        return 0


async def probe_detail(browser, port: int, job_id: str) -> str:
    """用浏览器读一条 job_detail，返回诊断标签。这是判出口可用性的唯一可靠办法。

      "ok"      读到岗位正文，出口可用
      "verify"  落到 verify.html「安全验证」页，需人工过（IP 被标记，可救）
      "blocked" 落到 403.html?code=31，**IP 硬封，人工验证无效，只能等解封**
      "login"   落到登录页
      "dead"    隧道不通 / 打不开
      "thin"    标题对了但正文没填充（竞态，可重试）

    ⚠️ 按 **URL** 判而不是按标题：403 页的标题只有「BOSS直聘」，既不含
    「请稍候」也不含「验证」，标题排除法会把它当成正常页面。
    """
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        proxy={"server": f"socks5://127.0.0.1:{port}"})
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        start = loop.time()
        title = ""
        while loop.time() - start < 40:
            cur = page.url or ""
            if "/403.html" in cur or "code=31" in cur:
                # 硬封。正文里写着解封时间，打出来省得人猜。
                try:
                    body = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''")
                    for line in body.splitlines():
                        if "恢复正常" in line or "当前IP" in line:
                            print(f"    {line.strip()}", flush=True)
                except Exception:
                    pass
                return "blocked"
            if "/verify.html" in cur:
                return "verify"
            try:
                title = await page.title()
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if title.strip() and "招聘" in title and "注册登录" not in title:
                break
            if "安全验证" in title:
                return "verify"
            if "注册登录" in title:
                return "login"
            await asyncio.sleep(1.5)
        if not title.strip():
            return "dead"
        if "招聘" not in title:
            return "verify" if "验证" in title else "dead"
        for _ in range(8):
            try:
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if len(text) > 500:
                return "ok"
            await asyncio.sleep(0.8)
        return "thin"
    finally:
        await ctx.close()


async def wait_manual_verify(port: int, tag: str, job_id: str,
                             wait: float = 300.0) -> bool:
    """弹一个可见浏览器让人工过验证。轮询页面自行判断，不用 input()
    （agent 工具环境 stdin 是关闭的，input() 会立刻 EOFError 让窗口秒退）。"""
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    print()
    print("!" * 68)
    print(f"出口 {tag}(:{port}) 已被 BOSS 标记，需要人工过一次验证。")
    print("浏览器即将弹出，请点掉「安全验证」；看到岗位详情出现即可，无需按键。")
    print("⚠️ 不要在这个窗口里登录账号。")
    print("!" * 68, flush=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            proxy={"server": f"socks5://127.0.0.1:{port}"})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"  打开失败: {exc}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait
        ok = False
        last = 0.0
        renav = 0
        while loop.time() < deadline:
            try:
                t = await page.title()
            except Exception:
                await asyncio.sleep(2.0)
                continue

            # 岗位页 = 验证已过且落到了目标页
            if "招聘" in t and "注册登录" not in t:
                ok = True
                print(f"  ✅ 已通过：{t!r}")
                break

            # ⚠️ 验证通过后 BOSS 常把你留在登录页（或首页），它**不会自己跳回岗位页**。
            # 这不是「出口受限」（我一度这样误判）——风控 cookie 已经拿到了，
            # 只是需要主动再导航一次。所以这里重新 goto，而不是继续傻等。
            if "注册登录" in t or "安全验证" not in t:
                if renav < 6:
                    renav += 1
                    print(f"  验证已过，重新打开岗位页（第 {renav} 次）", flush=True)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    await asyncio.sleep(3.0)
                    continue

            if loop.time() - last > 20:
                print(f"  等待中… {t!r}  剩余 {int(deadline - loop.time())}s", flush=True)
                last = loop.time()
            await asyncio.sleep(2.0)
        # 只保存风控 cookie，检测到登录态就拒绝（守卫与 manual_verify_proxy 一致）
        cookies = (await ctx.cookies()) or []
        bad = sorted({c["name"] for c in cookies} &
                     {"wt2", "wt", "geek_zp_token", "zp_at", "bst"})
        if bad:
            print(f"  ⚠️ 检测到登录 cookie {bad}，不保存 state（链路必须免登录）")
        elif ok:
            sp = ROOT / "data" / f"proxy_state_{tag}.json"
            await ctx.storage_state(path=str(sp))
            print(f"  已存 {len(cookies)} 条风控 cookie → {sp.name}")
        await ctx.close()
        await browser.close()
    if not ok:
        print(f"  ⏰ {wait:.0f}s 内未通过。")
    return ok
async def load_and_read(page, job_id: str, max_wait: float = 45.0) -> tuple[bool | None, str]:
    """加载详情页，返回 (是否关闭, 页面文本)。None = 未判定。"""
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    # 波尔链路实测单次首页往返 13-15s，30s 的 goto 超时在这里偏紧。
    # goto 超时不直接返回：挑战跳转常在 goto 报超时后才完成，
    # 后面的标题轮询仍有机会等到岗位页（实测探针就是这样第一条失败、第二条起正常）。
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    # 等挑战页自解。判据要同时排除三种中间态，只认「标题就是岗位标题」：
    #   · 「请稍候」   —— 挑战页本体
    #   · 「安全验证」 —— 需人工点击的那种（IP 被标记）
    #   · 「Loading …_security_check=…」 —— 挑战通过后的跳转中间态。
    #     ⚠️ 这个最坑：它既不含「请稍候」也不含「验证」，旧判据会误认为已通过，
    #     于是立刻 evaluate，撞上 "Execution context was destroyed"（页面正在导航），
    #     结果把本来能读到的岗位记成「未判定」。实测这是未判定的主因，与隧道无关。
    # 岗位页标题稳定含「招聘」，用它当正向判据比排除法可靠。
    loop = asyncio.get_event_loop()
    start = loop.time()
    ready = False
    while loop.time() - start < max_wait:
        try:
            title = await page.title()
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if title.strip() and "招聘" in title and "注册登录" not in title:
            ready = True
            break
        if "注册登录" in title:
            # 被标记的 IP 会被导到登录页。这条链路必须免登录，直接判未判定。
            return None, ""
        await asyncio.sleep(1.5)
    if not ready:
        return None, ""

    # 标题就位后 body 仍可能未填充（domcontentloaded 早于内容），再等 DOM。
    # evaluate 期间仍可能遇到导航，所以异常要重试而不是直接放弃。
    text = ""
    for _ in range(10):
        try:
            text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''")
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if len(text) > 500:
            break
        await asyncio.sleep(0.8)
    if len(text) < 500:
        return None, text
    return _is_closed(text), text


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=120,
                    help="本次最多处理多少条（默认 120，一轮人工验证大约撑这么多）")
    ap.add_argument("--all", action="store_true", help="不限条数，跑到没有为止")
    ap.add_argument("--include-no-jd", action="store_true",
                    help="也刷还没抓 JD 的岗位（默认只刷已进投递池的）。"
                         "默认关闭的理由：一条刷新 14 次请求，而无 JD 的岗位"
                         "页面上看不见，刷了没收益 —— 实测占了 70%")
    ap.add_argument("--stale-days", type=int, default=60,
                    help="活跃度换算天数 >= 此值即降级为 sample")
    ap.add_argument("--rotate-every", type=int, default=15, help="每 N 条换出口")
    ap.add_argument("--burst", type=int, default=25, help="每 N 条一次长停顿")
    # 慢速长跑档。默认值对齐老链路实测的安全速率：
    # 08-11 那轮（未触发封禁）是均值 6.7、峰值 12.5 请求/分钟，
    # 而一条岗位页 = 14 次请求 → 要压到 6 请求/分钟以下，条间需 ~140s。
    ap.add_argument("--gap", type=float, nargs=2, metavar=("LO", "HI"),
                    default=[110.0, 170.0],
                    help="条间间隔秒区间（默认 110-170s ≈ 6 请求/分钟）")
    ap.add_argument("--burst-pause", type=float, nargs=2, metavar=("LO", "HI"),
                    default=[420.0, 900.0],
                    help="每 --burst 条后的长停顿秒区间（默认 7-15 分钟）")
    ap.add_argument("--verify-wait", type=float, default=300.0,
                    help="每次弹窗等待人工验证的秒数")
    ap.add_argument("--verify", action="store_true",
                    help="遇到可救的 verify 状态时弹窗让人工过。"
                         "默认关闭——被限制期间每访问一次就把解封时间"
                         "重置为「此刻+24h」，所以只在你确实要守着过验证时开")
    ap.add_argument("--daily-cap", type=int, default=1000,
                    help="单日累计 HTTP 请求上限（跨脚本，靠 request_log 执行）。"
                         "注意一条岗位页折算 %d 次请求" % REQ_PER_JOB)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # ⚠️ 只刷**已在投递池里**（有 JD）的岗位，且按快照年龄从旧到新。
    #
    # 一条刷新 = 14 次请求，是整个项目最贵的单位操作。而实测（2026-08-30）
    # 原先的选行取 2703 条，其中 **1896 条（70%）没有 JD** —— 它们还没进池，
    # 页面上根本看不见，刷了活跃度没有任何收益。全刷需要 37842 次请求，
    # 只刷有 JD 的需要 11298 次，省下 26544 次。
    #
    # 排序也从 `canonical_id`（等于随机）改成按快照年龄降序：有 JD 的 807 条里
    # 345 条是 3 天内刚抓的（不必刷），462 条超 15 天（真需要刷）。
    # 按 id 排会让每轮都从同一批开始，最旧的那些永远轮不到。
    #
    # `--include-no-jd` 保留手动开关：如果哪天想给无 JD 的岗位刷状态
    # （比如先判死活再决定要不要花详情配额），显式打开。
    where = ("WHERE retention_status='active' AND job_invalid=0 "
             "  AND job_status_desc != '职位已关闭' ")
    if not args.include_no_jd:
        where += "  AND job_description != '' "
    rows = con.execute(
        "SELECT canonical_id, title, company_name, source_job_id FROM jobs "
        + where +
        # NULL 最旧，排最前；detail_fetched_at 有 Z 与 +00:00 两种后缀，
        # 但前 10 位都是日期，按字符串升序即年龄降序。
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC"
    ).fetchall()
    if not args.all:
        rows = rows[: args.batch]

    # 先按请求数体检，别等跑起来才发现今天早就超了。
    used = used_today(con)
    room = max(0, args.daily_cap - used)
    affordable = room // REQ_PER_JOB
    print(f"[账本] 今日已用 {used} 次请求 / 上限 {args.daily_cap}，"
          f"余量 {room} 次 ≈ {affordable} 条（每条约 {REQ_PER_JOB} 次）")
    if affordable <= 0:
        print("今日请求额度已用尽，明天再跑。封禁按累计请求量算，不是按条数。")
        return
    if len(rows) > affordable:
        print(f"本轮按额度截到 {affordable} 条（原 {len(rows)} 条）")
        rows = rows[:affordable]

    # 把节奏换算成请求/分钟打出来，和老链路的安全速率对齐着看。
    # 只报"条间 X 秒"没有意义——风控数的是请求数，一条是 14 次。
    gap_avg = sum(args.gap) / 2
    bp_avg = sum(args.burst_pause) / 2
    per_job = gap_avg + (bp_avg - gap_avg) / max(args.burst, 1)
    rpm = REQ_PER_JOB * 60 / per_job
    eta_h = len(rows) * per_job / 3600
    print(f"本轮待检测: {len(rows)} 条 ≈ {len(rows) * REQ_PER_JOB} 次请求")
    print(f"[节奏] 条间 {args.gap[0]:.0f}-{args.gap[1]:.0f}s，"
          f"每 {args.burst} 条停 {args.burst_pause[0]/60:.0f}-"
          f"{args.burst_pause[1]/60:.0f} 分钟")
    print(f"[节奏] 折算 {rpm:.1f} 请求/分钟，预计耗时 {eta_h:.1f} 小时"
          f"（参照：08-11 未触发封禁那轮均值 6.7、峰值 12.5 请求/分钟）")
    if not rows:
        return

    closed = unknown = refreshed = stale = done = 0
    streak = 0
    ei = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,   # 必须有头，无头挑战不自解
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])

        # 开跑前体检：**必须真读一条 job_detail**，不能只看首页返回码。
        # 首页 200 而详情页全是「安全验证」是实测存在的状态（见文件头注释），
        # 用首页判会把被标记的出口当成可用，然后一路空转 10 条才发现。
        usable: list[tuple[str, int]] = []
        for tag, port in EXITS:
            # 先用 forbidden-info 接口问一句（2 秒），能明确排除的就不必起浏览器
            # 读一条详情页（90 秒）。硬封的出口连弹窗都不该弹。
            verdict, note = exit_status(port)
            print(f"[出口] {tag}(:{port}) {verdict} — {note}", flush=True)
            if verdict == "dead":
                print(f"       起隧道: ssh -f -N -D {port} "
                      f"-o ServerAliveInterval=30 bohrium-{tag}", flush=True)
                continue
            if verdict == "blocked":
                print(f"       硬封无人工出路（页面只给「登录后使用」，"
                      f"而本链路必须免登录），跳过", flush=True)
                continue
            if verdict == "verify" and not args.verify:
                # ⚠️ 不要在这里起浏览器"试试看"：被限制期间每访问一次
                # （浏览器加载 job_detail/verify 页），解封时间就重置成
                # 此刻 +24h。实测 container 因此被推后 1.5 小时。
                # 只有 --verify 明确要求时才弹窗，否则直接跳过。
                print(f"       ⚠️ 被限制期间访问会把解封时间重置为「此刻+24h」。"
                      f"要人工过验证请加 --verify", flush=True)
                continue
            if verdict == "ok":
                # 接口说没限制，才起浏览器真读一条确认（接口只答 IP 级状态，
                # 不保证详情页此刻一定能读到）。
                verdict = await probe_detail(browser, port, rows[0]["source_job_id"])
                print(f"[出口] {tag} 详情页复检 {verdict}", flush=True)
            if verdict == "ok":
                usable.append((tag, port))
            elif verdict == "blocked":
                print(f"[出口] {tag} IP 已被硬封，跳过", flush=True)
            elif verdict == "thin":
                # 标题是岗位页但正文没填充 = 竞态，不是风控，可以用
                print(f"[出口] {tag} 正文未填充（竞态，非风控），纳入可用",
                      flush=True)
                usable.append((tag, port))
            elif verdict in ("verify", "login") and args.verify:
                if await wait_manual_verify(port, tag, rows[0]["source_job_id"],
                                            args.verify_wait):
                    again = await probe_detail(browser, port,
                                              rows[0]["source_job_id"])
                    print(f"[出口] {tag} 验证后复检 {again}", flush=True)
                    if again == "ok":
                        usable.append((tag, port))
        if not usable:
            print("没有可用出口，退出。")
            await browser.close()
            return
        print(f"可用出口: {[t for t, _ in usable]}", flush=True)

        async def new_ctx(idx: int):
            tag, port = usable[idx % len(usable)]
            kw = {"viewport": {"width": 1366, "height": 768},
                  "proxy": {"server": f"socks5://127.0.0.1:{port}"}}
            sp = ROOT / "data" / f"proxy_state_{tag}.json"
            if sp.exists():
                kw["storage_state"] = str(sp)
            c = await browser.new_context(**kw)
            return c, (c.pages[0] if c.pages else await c.new_page()), tag, port

        ctx, page, cur_tag, cur_port = await new_ctx(ei)
        print(f"[出口] 使用 {cur_tag}", flush=True)

        for i, r in enumerate(rows, 1):
            if i > 1 and (i - 1) % args.rotate_every == 0 and len(usable) > 1:
                await ctx.close()
                ei += 1
                ctx, page, cur_tag, cur_port = await new_ctx(ei)
                print(f"[出口] 切到 {cur_tag}", flush=True)
                await asyncio.sleep(2.0)

            result, text = await load_and_read(page, r["source_job_id"])
            done += 1
            log_requests(con, REQ_PER_JOB,
                         outcome="ok" if result is not None else "unknown")

            if result is None:
                unknown += 1
                streak += 1
                print(f"[未判定] {r['title'][:24]} | {r['company_name']}", flush=True)
                # 连续失败说明出口被标记了，停下来救，而不是继续空转。
                # 诊断也要用详情页判据，首页码只够回答隧道通不通。
                if streak >= FAIL_STREAK:
                    await ctx.close()
                    verdict, note = exit_status(cur_port)
                    print(f"[诊断] 连续 {streak} 条未判定，{cur_tag} {verdict} — {note}",
                          flush=True)
                    if verdict == "dead":
                        print(f"隧道断了。重起: ssh -f -N -D {cur_port} "
                              f"-o ServerAliveInterval=30 bohrium-{cur_tag}")
                        print("本轮停止。已处理的结果都已落库。")
                        break
                    if verdict == "ok":
                        # 接口说正常但读不出来，多半是我这边的竞态，换个上下文重试
                        verdict = await probe_detail(browser, cur_port,
                                                    r["source_job_id"])
                        print(f"[诊断] 详情页复检 {verdict}", flush=True)
                    if verdict == "blocked":
                        # 硬封无人工出路，把这个出口从池里摘掉；池空就收工。
                        usable[:] = [(t, p) for t, p in usable if p != cur_port]
                        print(f"{cur_tag} IP 已被硬封（见上面的解封时间），移出出口池",
                              flush=True)
                        if not usable:
                            print("所有出口都不可用，本轮停止。已处理的结果都已落库。")
                            break
                        streak = 0
                        ei += 1
                        ctx, page, cur_tag, cur_port = await new_ctx(ei)
                        continue
                    if verdict in ("verify", "login"):
                        ok = await wait_manual_verify(
                            cur_port, cur_tag, r["source_job_id"], args.verify_wait)
                        if not ok:
                            print("未通过验证，本轮停止。已处理的结果都已落库。")
                            break
                        if await probe_detail(browser, cur_port,
                                             r["source_job_id"]) != "ok":
                            print(f"{cur_tag} 验证后仍读不到岗位页，本轮停止。"
                                  "已处理的结果都已落库。")
                            break
                    streak = 0
                    ctx, page, cur_tag, cur_port = await new_ctx(ei)
                    continue
            else:
                streak = 0

            if result is True:
                con.execute(
                    "UPDATE jobs SET job_invalid=1, job_status_desc='职位已关闭', "
                    "retention_status='sample' WHERE canonical_id=? "
                    "AND retention_status='active'", (r["canonical_id"],))
                con.commit()
                closed += 1
                print(f"[关闭] {r['title'][:24]} | {r['company_name']}", flush=True)
            elif result is False and text:
                phrase, days = parse_active_status(text)
                if phrase:
                    con.execute(
                        "UPDATE jobs SET recruiter_active_status=?, recruiter_online=?, "
                        "updated_at=? WHERE canonical_id=?",
                        (phrase, 1 if phrase == "在线" else 0,
                         datetime.now().isoformat(timespec="seconds"),
                         r["canonical_id"]))
                    refreshed += 1
                    if days is not None and days >= args.stale_days:
                        # 降级 != 关闭：岗位还在招，只是招聘者久未上线，投递价值低。
                        # 留在库里做增量去重，不写 job_invalid。bookmarked 的不动。
                        con.execute(
                            "UPDATE jobs SET retention_status='sample' "
                            "WHERE canonical_id=? AND retention_status='active' "
                            "AND bookmarked=0", (r["canonical_id"],))
                        stale += 1
                        print(f"[不活跃-{phrase}] {r['title'][:22]} | "
                              f"{r['company_name']}", flush=True)
                    con.commit()

            if i % 10 == 0 or i == len(rows):
                print(f"进度 {i}/{len(rows)}  关闭{closed} 未判定{unknown} "
                      f"活跃度{refreshed} 降级{stale}", flush=True)

            if i < len(rows):
                if args.burst and i % args.burst == 0:
                    bp = random.uniform(*args.burst_pause)
                    print(f"[节奏] 长停顿 {bp/60:.1f} 分钟", flush=True)
                    await asyncio.sleep(bp)
                else:
                    await asyncio.sleep(random.uniform(*args.gap))

        await ctx.close()
        await browser.close()

    print(f"\n本轮完成: 处理 {done}，关闭 {closed}，未判定 {unknown}，"
          f"活跃度更新 {refreshed}，长期不活跃降级 {stale}")
    left = con.execute(
        "SELECT count(*) FROM jobs WHERE retention_status='active' "
        "AND job_invalid=0 AND job_status_desc != '职位已关闭'").fetchone()[0]
    print(f"剩余待检测: {left} 条")
    con.close()


if __name__ == "__main__":
    # 不设 WindowsSelectorEventLoopPolicy：selector 循环不支持子进程，
    # 而 Playwright 要 spawn driver（NotImplementedError）。
    asyncio.run(main())
