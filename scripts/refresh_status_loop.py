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

⚠️ **成本认知（2026-08-30 实测重写，原先的数字全部偏离）**：

| 取法 | 每条请求数 |
|---|---:|
| `page.goto` 整页加载 | **102**（图片 62、xhr 17、script 14…） |
| 整页 + 拦静态资源 | 39，但风控域 `www.zhipin.com` 只从 21 降到 20 |
| **同源 fetch HTML**（现用） | **1**，摊薄含 stoken 重算约 **1.58** |

原先文件头写「一条 13 次以上」，那个数是从一张 **verify 页**数出来的 ——
真实岗位页是 102 次，**低估了 7 倍**。而账本里 `detail_page` 一行都没有，
说明这条链路自接入账本后从未实跑过，所以那个假设一直没被现实检验。

现在走 `fetch()`：它**不加载子资源**，只发一个文档请求。整页那 20 次自家域请求里
17 次是 SPA 自己的壳（`city.json`/`industry.json`/banner/popupguides/
agreement-tip/resume-parser/smsSwitch…），读 JD 一个都不需要。
详见 `src/job_radar/refresh_fetch.py` 与 `data/calibration/`。

**仍然：估算风险要按请求数，不能按条数**（08-27 被封的根因）。
只是现在一条真的只值 1-2 次，而不是十几次。

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

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import parse_active_status, _is_closed  # noqa: E402
from job_radar.exits import UA, exit_pool, exit_verdict  # noqa: E402
from job_radar.refresh_fetch import (  # noqa: E402
    DEFAULT_WINDOW_POSITION, EXTRACT_JS, FETCH_WINDOW, MIN_JOB_HTML_LEN,
    REQ_PER_RECOVER, is_closed_status, launch_args, quota_factor, same_job,
)

# 一条 fetch = 1 次请求。窗口耗尽后重算 stoken 约 3 次，窗口约 5 条 →
# 摊薄 1.6 次/条。账本按**实际发生**记（fetch 记 1，重算记 3），
# 不再用一个写死的估计值。
REQ_PER_JOB = 1

# 主动重暖阈值的**下限**。推导见主循环里的注释：每条期望成本
# = 1 + REQ_PER_RECOVER/N，而等撞墙是 1 + 5/W。N 低于 5 就已经不比等撞墙便宜，
# 低到 2 时（2.50 次/条）比什么都不做更糟。
# 实测代价：第一版下限写 2，那轮 30 条跑出 **4.13 次/条**，
# 而前一轮固定阈值 5 只要 1.68 —— "减少浪费"的机制自己成了最大的浪费。
MIN_WINDOW = 5
# 同一出口连续撞墙多少次就换出口。窗口是 **IP 的属性**，
# 反复重算换不回一个已经在收缩的窗口。
EXIT_STRIKE_LIMIT = 3

# 重算 stoken 后要等多久才 fetch。**下界不能小于 5s**（2026-08-30 实测）：
# 扫 0/1/2/3/5/8s 各 3 次，成功率 0% / 33% / 100% / 67% / 100% / 100%。
# 趋势可信（0s 必失败、≥5s 必成功），但 **2s 那个 100% 与 3s 的 67% 是噪声**
# —— 每档只 3 次，单次翻转就能造出这种假差异，所以不要照着 2s 去优化。
# 原值是 3-5s（随手取的，从没量过），下界偏小。
# 探针 `scripts/probe_stoken_recovery.py`，数据 `data/calibration/stoken-recovery.json`。
REWARM_WAIT = (5.0, 7.0)

PROBE_URL = "https://www.zhipin.com/"

# ⚠️ 首页返回码是**假绿灯**，不要用它判「能不能读岗位页」（2026-08-27 实测推翻）。
#    实测同一时刻：首页 200，而 job_detail 每一条都落到「安全验证」页（len=101）。
#    连续 6 次、间隔 20s 也全部如此，说明不是配额窗口而是详情页级别的标记。
#    首页几乎不设防，被标记的 IP 照样 200 —— 所以它只能回答「隧道通不通」。
#    唯一可靠判据是**用浏览器真的读一条 job_detail**，见 probe_detail()。
#    裸 curl 打 job_detail 也不行：未被标记的 IP 同样会 302 到 security.html
#    （那个 302 就是 JS 挑战本身，浏览器会跟过去解掉再回来），
#    所以裸 curl 无法区分「正常挑战」和「已被标记」。


# 连续这么多条未判定就怀疑出口失效（先探再判，别急着弹窗）。
FAIL_STREAK = 10


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

            # ⚠️ **判据取「风控 cookie 到手」，不取「能看到岗位页」**（2026-08-31 修）。
            # 原先要求标题含「招聘」才算过，那把两件事混成了一件：
            #   ① 人过掉了验证（拿到 __zp_stoken__ / 风控 cookie）
            #   ② 匿名访客能看岗位页
            # 这条链路是免登录的，②**本来就可能再次要求验证**。于是循环变成：
            # 人点完 → 脚本立刻 goto 岗位页 → 平台又要验证 → 判定"还没过" →
            # 再 goto → 用户眼里就是「验证组件反复加载」，最多 6 次。
            # 用户实测反馈：「我验证成功后一直重复加载验证组件」——就是这个。
            # 人的劳动在第一次就已经完成了，不该被后续导航结果否定。
            names = {c["name"] for c in (await ctx.cookies())}
            if "__zp_stoken__" in names:
                ok = True
                print(f"  ✅ 已通过：拿到风控 cookie（当前页 {t!r}）")
                break

            # 没拿到 cookie 时才重新导航，且**次数收紧到 2**：
            # 每次 goto 都是一次真实请求，多撞只会加重限制。
            if "注册登录" in t or "安全验证" not in t:
                if renav < 2:
                    renav += 1
                    print(f"  还没拿到风控 cookie，重开岗位页（第 {renav}/2 次）",
                          flush=True)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    await asyncio.sleep(3.0)
                    continue
                # 两次都没拿到就别再撞了 —— 让人知道这个窗口点了也没用。
                print("  ⚠️ 重开两次仍未拿到风控 cookie，这个出口当前过不去。"
                      "别再点了，等平台解封或换出口。", flush=True)
                break

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
async def warm_stoken(page, ctx, job_id: str, timeout: float = 60.0) -> bool:
    """整页加载一条，等 `__zp_stoken__` 就位。fetch 全靠这个会话 cookie。

    判据是**cookie 真的出现**，不是标题像不像岗位页：
    关闭岗位的标题同样含「招聘」（实测「…工程师招聘工资_…-BOSS直聘」），
    而 stoken 才是后面 fetch 实际依赖的东西 —— 直接检查它，
    比从标题反推「大概拿到了」可靠。这个教训是探针阶段真踩到的：
    按标题判时冷启动被误报为失败，整轮探测直接退出。
    """
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass  # 挑战跳转常在 goto 报超时后才完成，继续等 cookie
    loop = asyncio.get_event_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        try:
            names = {c["name"] for c in await ctx.cookies()}
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if "__zp_stoken__" in names:
            return True
        cur = page.url or ""
        if "/verify.html" in cur or "/403.html" in cur or "code=31" in cur:
            return False
        await asyncio.sleep(1.0)
    return False


async def fetch_job(page, job_id: str, attempts: int = 3) -> dict:
    """同源 fetch 一条岗位页并按容器取值。返回 refresh_fetch.EXTRACT_JS 的结果。

    ⚠️ evaluate 期间页面可能正在导航（重算 stoken 之后尤其常见），
    Playwright 会抛 "Execution context was destroyed"。这不是风控，
    等一下重试即可 —— 采集侧 `boss.py:_evaluate_fetch` 有同一处容错。
    """
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    for attempt in range(1, attempts + 1):
        try:
            return await page.evaluate(EXTRACT_JS,
                                       {"url": url, "minLen": MIN_JOB_HTML_LEN})
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                return {"ok": False, "reason": "evaluate_error", "error": str(exc)}
            await asyncio.sleep(2.5)
    return {"ok": False, "reason": "evaluate_error"}


async def load_and_read(page, job_id: str, max_wait: float = 45.0) -> tuple[bool | None, str]:
    """【整页加载版，已不用于跑批】返回 (是否关闭, 页面文本)。None = 未判定。

    保留原因：它是「真浏览器带 CSS 渲染」的口径，`body.innerText` 就是用户
    看到的东西，可作为 fetch 判定的对照基准（探针 `probe_closed_truth.py`
    正是用它验出 `.job-status` 在招时是「招聘中」的）。
    跑批不要用：实测一条 102 次请求，是 fetch 的 100 倍。
    """
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
                         "无 JD 的岗位在投递池页面上看不见，刷了拿不到可用结论；"
                         "它们曾占待刷队列的七成。")
    ap.add_argument("--stale-days", type=int, default=60,
                    help="活跃度换算天数 >= 此值即降级为 sample")
    ap.add_argument("--skip-fresh-hours", type=float, default=12.0,
                    help="跳过 updated_at 在 N 小时内的行（默认 12，0=关闭）。"
                         "无人值守续跑必须开：见选行处的注释")
    # 越线行为做成开关：日线是我们自设的预算（不是平台给的数字），
    # 越线后该停还是该降速取决于当次任务目标 —— 摸边界时该停，赶进度时降速跑完。
    # 默认 stop 保持历史行为，不让既有调用方被动改变语义。
    ap.add_argument("--over-quota", choices=("stop", "slow"), default="stop",
                    help="越过 --daily-cap 后：stop=停机（默认），slow=降速跑完")
    # 出口是**策略**：哪个能用取决于当下平台对它的态度，不该写死。
    # 实测（2026-08-31）两个出口状态可以完全不同 —— vm 正常直达岗位页，
    # 而 container 在自解后被要求二次验证。混用会让好出口被坏出口连累
    # （撞墙 3 次就轮换过去，然后一路空转）。
    ap.add_argument("--only-exit", default="",
                    help="只用指定出口（vm / container），留空=全部。"
                         "一个出口坏掉时用它单跑好的那个")
    ap.add_argument("--skip-exit", default="",
                    help="跳过这些出口（逗号分隔）。出口多于两个时比 --only-exit 好写")
    ap.add_argument("--slow-ladder", default="1.0:2,1.25:3,1.5:4,2.0:6",
                    help="降速档位 用量倍数:延迟乘数，逗号分隔（配合 --over-quota slow）")
    ap.add_argument("--rotate-every", type=int, default=15, help="每 N 条换出口")
    ap.add_argument("--burst", type=int, default=25, help="每 N 条一次长停顿")
    # 节奏档。**随成本重算过**：原先条间 110-170s 是按「一条 14 次请求」定的，
    # 目标压到 6 请求/分钟以下。现在一条摊薄 1.6 次，同样的 6 请求/分钟只需
    # 条间约 16s。这里取 18-32s（均值 25s ≈ 3.8 请求/分钟），比老链路
    # 08-11 那轮（均值 6.7、未触发封禁）还保守一半，而 807 条从 53 小时降到约 6 小时。
    #
    # ⚠️ 别把它再调快：真正的约束不是请求数而是 stoken 窗口 —— 每约 5 条就要
    # 重算一次，重算本身是导航（比 fetch 重）。间隔太小只会让重算更密。
    # ⚠️ 提速的收益上限由 stoken 窗口决定，不由间隔决定。实测（2026-08-30 阶梯探测）：
    #   12-18s → 1.67 请求/条、挑战率 14%、日线可跑 598 条
    #    8-12s → 1.92 请求/条、挑战率 29%、日线可跑 520 条
    # 也就是**间隔压到 8-12s 反而少跑 78 条**：挑战页是白花的请求，
    # 密度上去之后吃掉的比省下的时间更多。所以下限守在 12s。
    ap.add_argument("--gap", type=float, nargs=2, metavar=("LO", "HI"),
                    default=[18.0, 32.0],
                    help="条间间隔秒区间（默认 18-32s ≈ 3.8 请求/分钟）")
    ap.add_argument("--burst-pause", type=float, nargs=2, metavar=("LO", "HI"),
                    default=[90.0, 180.0],
                    help="每 --burst 条后的长停顿秒区间（默认 1.5-3 分钟）")
    ap.add_argument("--verify-wait", type=float, default=300.0,
                    help="每次弹窗等待人工验证的秒数")
    ap.add_argument("--verify", action="store_true",
                    help="撞上可救的 verify 态时弹出浏览器让人工过（默认不弹）。"
                         "只在人确认在场时加：无人点的窗口零产出，还占着出口。"
                         "弹窗本身不会推后解封时刻，人点掉即当场解除。")
    ap.add_argument("--window-position", default=DEFAULT_WINDOW_POSITION,
                    help="浏览器窗口落点 x,y（默认 %(default)s，主屏之外不抢焦点）。"
                         "传空字符串则开在屏幕中央。"
                         "⚠️ 坐标必须为正——负坐标会被挑战 JS 判为异常环境。")
    _amort = REQ_PER_JOB + REQ_PER_RECOVER / FETCH_WINDOW
    ap.add_argument("--daily-cap", type=int, default=1000,
                    help=f"单日累计 HTTP 请求上限（本链路独立账本，"
                         f"kind='detail_page'）。一条摊薄约 {_amort:.1f} 次请求"
                         f"（1 次 fetch + 每 {FETCH_WINDOW} 条一次 "
                         f"{REQ_PER_RECOVER} 次重算），所以 1000 次约合 600 条")
    args = ap.parse_args()

    # 解析降速档位。`--over-quota stop` 时传空表 → quota_factor 恒返回 1.0，
    # 不需要在每个使用点再判一次模式。
    ladder: list[tuple[float, float]] = []
    if args.over_quota == "slow":
        for part in args.slow_ladder.split(","):
            thr, _, mult = part.partition(":")
            ladder.append((float(thr), float(mult)))

    # timeout：三条链路（刷新 / 补详情 / 标签重打）会并行写同一个库。
    # 库本身是 WAL（允许一写多读 + 写者排队），但**默认 5s 的 busy_timeout
    # 在三方争抢下不够** —— 超时会抛 "database is locked" 把整批打断，
    # 而刷新是花了配额才拿到的结果，中途崩掉等于白花。
    # 30s 足够让另一方的短事务（都是单行 UPDATE + commit）让开。
    con = sqlite3.connect(DB, timeout=30.0)
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
    # ⚠️ **排序键（detail_fetched_at）不是本脚本写的列**，所以刷完一条之后
    # 它在队列里的位置**不变** —— 下一次启动会从同一批头部重新开始。
    # 手工按批跑时看不出来（人记得跑到哪了），但无人值守续跑会把额度
    # 反复花在同一批上：实测 2026-08-31 队头 200 条里 153 条是 6 小时内
    # 刚刷过的，直接重起等于 76% 的配额买重复结论。
    #
    # 判据用 `updated_at`（本脚本唯一真正写的时间戳），而不是给排序键补写值：
    # `detail_fetched_at` 的语义是「详情抓取时间」，刷新并没有抓详情，
    # 借用它会让补详情链路的选行跟着漂。
    if args.skip_fresh_hours > 0:
        where += (f"  AND (updated_at IS NULL OR updated_at = '' OR "
                  f"updated_at < datetime('now','-{args.skip_fresh_hours} hours')) ")
    rows = con.execute(
        "SELECT canonical_id, title, company_name, source_job_id FROM jobs "
        + where +
        # NULL 最旧，排最前；detail_fetched_at 有 Z 与 +00:00 两种后缀，
        # 但前 10 位都是日期，按字符串升序即年龄降序。
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC"
    ).fetchall()
    if args.skip_fresh_hours > 0:
        # 把跳过量打出来。这条数字是「续跑有没有在重复劳动」的唯一现场判据：
        # 它接近队列总量说明本轮几乎无事可做（正常），而它长期为 0 则说明
        # 判据失效（比如哪天有人给 updated_at 换了写入口径）。
        # ⚠️ 这里**重新拼一遍条件**而不是对上面的 where 做字符串替换：
        # 那个条件是 `(IS NULL OR = '' OR < 时刻)`，只把 `<` 换成 `>=`
        # 会把 NULL/空 也一起数进"新鲜"里，虚报跳过量。
        fresh = con.execute(
            "SELECT count(*) FROM jobs WHERE retention_status='active' "
            "AND job_invalid=0 AND job_status_desc != '职位已关闭' "
            + ("AND job_description != '' " if not args.include_no_jd else "")
            + f"AND updated_at >= datetime('now','-{args.skip_fresh_hours} hours')"
        ).fetchone()[0]
        print(f"[选行] 跳过 {args.skip_fresh_hours:.0f}h 内已刷过的 {fresh} 条"
              f"（含补详情链路刚更新的——它同样写 recruiter_active_status，"
              f"再刷一次是白花配额）")
    if not args.all:
        rows = rows[: args.batch]

    # 先按请求数体检，别等跑起来才发现今天早就超了。
    # 摊薄成本 = 1 次 fetch + 每 FETCH_WINDOW 条一次 REQ_PER_RECOVER 次重算。
    per_job_req = REQ_PER_JOB + REQ_PER_RECOVER / FETCH_WINDOW
    used = used_today(con)
    room = max(0, args.daily_cap - used)
    affordable = int(room / per_job_req)
    print(f"[账本] 今日已用 {used} 次请求 / 上限 {args.daily_cap}，"
          f"余量 {room} 次 ≈ {affordable} 条（每条摊薄 {per_job_req:.2f} 次）")
    # 越线行为由 --over-quota 决定（stop=历史行为，slow=降速跑完）。
    # 1000 这个数不是平台给的，是我们按「未触发封禁的那轮」反推的自设预算，
    # 所以"越线怎么办"是策略而不是事实 —— 交给调用方，代码两种都支持。
    # 真正的风控信号（挑战页连撞、出口 code=31）在两种模式下都立刻停。
    if args.over_quota == "stop":
        if affordable <= 0:
            print("[BUDGET-STOP] 今日请求额度已用尽，明天再跑。无需人工介入。")
            return
        if len(rows) > affordable:
            print(f"本轮按额度截到 {affordable} 条（原 {len(rows)} 条）")
            rows = rows[:affordable]
    elif affordable <= 0:
        print(f"[OVER-QUOTA] 今日已用 {used}/{args.daily_cap}，降速继续（--over-quota slow）")
    elif len(rows) > affordable:
        print(f"[OVER-QUOTA] 额度只够 {affordable} 条，其余 "
              f"{len(rows) - affordable} 条降速跑完（--over-quota slow）")

    # 把节奏换算成请求/分钟打出来，和老链路的安全速率对齐着看。
    # 只报"条间 X 秒"没有意义——风控数的是请求数。
    gap_avg = sum(args.gap) / 2
    bp_avg = sum(args.burst_pause) / 2
    per_job = gap_avg + (bp_avg - gap_avg) / max(args.burst, 1)
    rpm = per_job_req * 60 / per_job
    eta_h = len(rows) * per_job / 3600
    print(f"本轮待检测: {len(rows)} 条 ≈ {len(rows) * per_job_req:.0f} 次请求"
          f"（整页加载同样条数需 {len(rows) * 102} 次）")
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
            args=launch_args(args.window_position),
            ignore_default_args=["--enable-automation"])

        # 开跑前体检：**必须真读一条 job_detail**，不能只看首页返回码。
        # 首页 200 而详情页全是「安全验证」是实测存在的状态（见文件头注释），
        # 用首页判会把被标记的出口当成可用，然后一路空转 10 条才发现。
        usable: list[tuple[str, int]] = []
        try:
            candidates = exit_pool(only=args.only_exit, skip=args.skip_exit)
        except ValueError as exc:
            print(f"[出口] {exc}")
            return
        if args.only_exit or args.skip_exit:
            print(f"[出口] 本轮只用 {[t for t, _ in candidates]}", flush=True)
        for tag, port in candidates:
            # 先用 forbidden-info 接口问一句（2 秒），能明确排除的就不必起浏览器
            # 读一条详情页（90 秒）。硬封的出口连弹窗都不该弹。
            verdict, note = exit_verdict(port)
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
                # ⚠️ 这里原先写着「被限制期间每访问一次，解封时间就重置成此刻+24h」
                # —— **那条是错的**，2026-08-28 已实测推翻（见 boss-job-radar.md
                # 「解封时间不是"每访问一次就 +24h"」）：
                #   · 硬封 IP 开浏览器读一条 job_detail，前后 expireTime 逐毫秒不变
                #   · verify 态 IP 人工点掉后，限制**当场解除**，不是推后
                # expireTime 是初次限制时定的固定时刻，不会因访问续期。
                # 08-27 看到的"推后 1.5 小时"更可能是那次访问触发了**新的**限制
                # 判定覆盖旧记录，而不是每次访问都续期。
                # 所以 verify 态**该起浏览器去救**，不必怕加时而干等。
                # 默认仍不自动弹窗（要人在场才有意义），但理由是"没人点等于白开"，
                # 不再是"会加时"。
                print(f"       verify 态可人工过（点掉即当场解除，不会加时）。"
                      f"要弹窗请加 --verify", flush=True)
                continue
            if verdict == "ok":
                # ⚠️ 这里**不再**跑 `probe_detail` 复检。
                # 它起一个无拦截的新 context 整页加载一条详情页 = 实测 102 次请求，
                # 两个出口就 204 次 —— 比一整批 100 条（约 160 次）还贵。
                # 而它的判据价值（「接口说 ok 但详情页读不出来」）现在由
                # 跑批前的 `warm_stoken` 覆盖：暖不起来就是读不了，
                # 而那次 goto 本来就要发（fetch 依赖它拿 stoken），**不额外花钱**。
                usable.append((tag, port))
            elif verdict == "blocked":
                print(f"[出口] {tag} IP 已被硬封，跳过", flush=True)
            elif verdict in ("verify", "login") and args.verify:
                if await wait_manual_verify(port, tag, rows[0]["source_job_id"],
                                            args.verify_wait):
                    again = await probe_detail(browser, port,
                                              rows[0]["source_job_id"])
                    print(f"[出口] {tag} 验证后复检 {again}", flush=True)
                    if again == "ok":
                        usable.append((tag, port))
        if not usable:
            # 统一的可 grep 标记，长跑监控只认它（见 backfill_details 的同名标记）
            print("[RISK-STOP] 没有可用出口（出口被标记或隧道不通），退出。")
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
            p = c.pages[0] if c.pages else await c.new_page()
            # 拦静态资源与埋点。只影响 `warm_stoken` 的那次整页导航
            # （fetch 本身不加载子资源，无所谓）—— 但那一次不拦就是 102 次请求，
            # 拦掉降到约 10 次。实测拦截省下的全在图片/CDN 域，
            # 风控主体域 www.zhipin.com 只从 21 降到 20，所以**省的是带宽和时间**；
            # 真正的降幅来自改用 fetch。
            # ⚠️ 绝不能拦 script/document/xhr：BOSS 的 JS 挑战靠自家脚本算
            # `__zp_stoken__`，拦掉脚本等于让挑战永远过不去。
            # `logapi.zhipin.com` 是 BOSS 自己的埋点上报，拦掉同时少交行为特征。
            async def _route(route):
                req = route.request
                host = (req.url.split("/")[2] if "//" in req.url else "").lower()
                if req.resource_type in ("image", "media", "font") or any(
                    b in host for b in ("logapi.zhipin.com", "geetest.com",
                                        "apm-fe.zhipin.com", "hm.baidu.com",
                                        "shink.zhipin.com", "t.zhipin.com")
                ):
                    await route.abort()
                else:
                    await route.continue_()
            await p.route("**/*", _route)
            return c, p, tag, port

        ctx, page, cur_tag, cur_port = await new_ctx(ei)
        print(f"[出口] 使用 {cur_tag}", flush=True)
        # fetch 依赖会话级 __zp_stoken__，换 context 后必须重新暖一次。
        warmed = await warm_stoken(page, ctx, rows[0]["source_job_id"])
        log_requests(con, REQ_PER_RECOVER, outcome="warm")
        print(f"[stoken] 冷启动 {'就绪' if warmed else '失败'}", flush=True)
        # ⚠️ **cookie 出现 ≠ 立刻可用**：REWARM_WAIT 的由来是实测
        # 「重算后等多久才生效」——0s→0%、1s→33%、5s→100%（见常量注释）。
        # 重暖路径一直有这个等待，**冷启动却漏了**，于是每轮第一条 fetch
        # 必然撞挑战页；撞墙又把 window 收一格、strikes +1，三次就换出口，
        # 换出口后的 warm 同样没等待 → 再撞 → 循环。
        # 2026-08-31 实测后果：整批 0 ok / 100% challenged，
        # 而同样的 EXTRACT_JS 在探针里（暖场后自然有间隔）4/4 成功。
        # 这就是那次被我误判成"IP 被封"的真实原因。
        if not warmed:
            print("拿不到 stoken，fetch 无从进行。本轮停止。")
            await ctx.close()
            await browser.close()
            return
        await asyncio.sleep(random.uniform(*REWARM_WAIT))
        since_warm = 0
        # 自适应窗口：初值取实测上界，撞墙即收缩（但不低于 MIN_WINDOW）、
        # 连续顺利则回升。窗口是**出口的属性**（随该 IP 累计用量收缩），
        # 换出口时要重置。
        window_now = FETCH_WINDOW
        clean_windows = 0
        exit_strikes = 0
        # 当前降速倍数。循环里每 10 条按**实际用量**复算一次。
        slow_factor = quota_factor(used, args.daily_cap, ladder)
        if slow_factor > 1.0:
            print(f"[OVER-QUOTA] 起步即越线，降速 {slow_factor:.0f}x", flush=True)

        for i, r in enumerate(rows, 1):
            # ⚠️ **日线必须在循环里复查**，不能只在启动时算一次。
            # 启动时的截断用的是摊薄估值 1.60 次/条，而实测（2026-08-31 本轮）
            # 真实是 2.04 次/条：挑战页重试 + 换出口重暖都不在那个估值里
            # （当轮 449 次请求里 ok 只占 274，其余是 challenged/warm/rewarm/prewarm）。
            # 于是启动时按 552 条放行，跑完实际会花约 1243 次 —— 超日线 24%。
            # 估值偏低不是可以接受的误差：日线是风控安全线，宁可少跑。
            #
            # 每 10 条查一次库（单次 count 走 idx_request_log_day，微秒级），
            # 而不是每条都查：这里要的是"别越线太多"，不是精确到单次请求。
            # 每 10 条复查一次实际用量，换算当前降速倍数。
            # ⚠️ 必须按**实际计数**复查而不是沿用启动估值：启动时按 1.60 次/条
            # 估，实测是 2.04 次/条（挑战页重试与换出口重暖不在估值里），
            # 沿用估值会让降速迟迟不触发。
            if i % 10 == 1:
                now_used = used_today(con)
                if args.over_quota == "stop" and now_used >= args.daily_cap:
                    print(f"\n[BUDGET-STOP] 日线到顶：已用 {now_used}/"
                          f"{args.daily_cap}（实测 {now_used/max(i-1,1):.2f} 次/条，"
                          f"启动估值 {per_job_req:.2f}）。已处理 {i-1}/{len(rows)} 条，"
                          f"结果都已落库。", flush=True)
                    print("[BUDGET-STOP] 无需人工介入，明日额度重置后可续跑。",
                          flush=True)
                    break
                new_factor = quota_factor(now_used, args.daily_cap, ladder)
                if new_factor != slow_factor:
                    slow_factor = new_factor
                    if slow_factor > 1.0:
                        print(f"\n[OVER-QUOTA] 已用 {now_used}/{args.daily_cap}"
                              f"（{now_used/max(args.daily_cap,1):.2f}x，实测 "
                              f"{now_used/max(i-1,1):.2f} 次/条），"
                              f"降速至 {slow_factor:.0f}x 继续。", flush=True)
            if i > 1 and (i - 1) % args.rotate_every == 0 and len(usable) > 1:
                await ctx.close()
                ei += 1
                ctx, page, cur_tag, cur_port = await new_ctx(ei)
                print(f"[出口] 切到 {cur_tag}", flush=True)
                await asyncio.sleep(2.0)
                # 新出口 = 新 context = 新会话，stoken 要重新暖。
                if not await warm_stoken(page, ctx, r["source_job_id"]):
                    print(f"[stoken] {cur_tag} 暖场失败，移出出口池", flush=True)
                    usable[:] = [(t, p) for t, p in usable if p != cur_port]
                    if not usable:
                        print("[RISK-STOP] 所有出口都不可用，本轮停止。已处理的结果都已落库。")
                        break
                    ei += 1
                    ctx, page, cur_tag, cur_port = await new_ctx(ei)
                    if not await warm_stoken(page, ctx, r["source_job_id"]):
                        print("备用出口也暖不起来，本轮停止。")
                        break
                log_requests(con, REQ_PER_RECOVER, outcome="warm")
                # 与冷启动同一个理由：暖完必须等 stoken 生效再 fetch，
                # 否则轮换后的第一条必然撞墙（0s 生效率实测为 0）。
                await asyncio.sleep(random.uniform(*REWARM_WAIT))
                since_warm = 0
                # 新出口的窗口是它自己的额度，回到上界重新摸。
                # strikes 也要清零：它记的是「当前出口」的不健康程度，
                # 跨出口累加会让刚换上的好出口立刻又被判定该换。
                window_now = FETCH_WINDOW
                clean_windows = 0
                exit_strikes = 0

            # 主动重暖：撞墙那一条**必然浪费一次请求**（拿回 41KB 挑战页，
            # 什么都判不出来），还要再花一次重试。与其等撞墙，不如在窗口边界前
            # 主动换 stoken —— 代价相同（都是一次重算），但省掉那两次浪费。
            #
            # ⚠️ 阈值**不能无限往下收**，因为重算本身值 3 次请求。
            # 每条期望成本 = 1 + REQ_PER_RECOVER/N：
            #   N=6 → 1.50   N=5 → 1.60   N=4 → 1.75   N=3 → 2.00   N=2 → 2.50
            # 而「等撞墙」的成本是 1 + 5/W（浪费 1 次挑战页 + 3 次重算 + 1 次重试），
            # W=5 时是 2.00。**所以 N 收到 4 以下就已经比等撞墙更贵**，
            # 收到 2 更是灾难。
            #
            # 这是实测教训：第一版下限写 2，那一轮（30 条）单条成本
            # **4.13 次**，是四轮里最差的 —— 而前一轮固定阈值 5 只要 1.68 次。
            # 阈值一旦被撞墙事件推到低位，重算就占掉总成本的 42%，
            # 「减少浪费」的机制自己变成了最大的浪费。
            # 所以下限定在 MIN_WINDOW=5：低于它就不该靠重算硬撑，
            # 该做的是换出口（那个 IP 的窗口已经不健康了）。
            if since_warm >= window_now:
                print(f"  [窗口] 已连打 {since_warm} 条（阈值 {window_now}），"
                      f"主动重算 stoken", flush=True)
                if await warm_stoken(page, ctx, r["source_job_id"]):
                    log_requests(con, REQ_PER_RECOVER, outcome="prewarm")
                    since_warm = 0
                    clean_windows += 1
                    # 连续两个窗口没撞墙就试着放宽一格（别一直停在收缩后的低位）
                    if clean_windows >= 2 and window_now < FETCH_WINDOW:
                        window_now += 1
                        clean_windows = 0
                    await asyncio.sleep(random.uniform(*REWARM_WAIT))

            rec = await fetch_job(page, r["source_job_id"])
            since_warm += 1
            log_requests(con, REQ_PER_JOB,
                         outcome="ok" if rec.get("ok") else "challenged")

            # 撞挑战页 = stoken 窗口用尽（实测每约 5.5 条一次，与采集链路的
            # code=37 同构）。重暖一次再重试这一条，而不是记未判定放过去 ——
            # 放过去等于这条白花一次请求且下轮还要再花。
            if not rec.get("ok") and rec.get("reason") == "challenged":
                print(f"  [窗口] 第 {since_warm} 条后撞挑战页，重算 stoken", flush=True)
                # 撞墙说明该出口的窗口在缩，但**收阈值的收益有下限**（见上方推导：
                # 低于 5 就比等撞墙更贵）。所以只收到 MIN_WINDOW 就不再收，
                # 并记一次该出口的不健康信号。
                window_now = max(MIN_WINDOW, window_now - 1)
                clean_windows = 0
                exit_strikes += 1
                # 同一出口反复撞墙 = 那个 IP 的配额窗口已经不健康，
                # 继续靠重算硬撑只会让重算占比越来越高（实测能到 42%）。
                # 换出口比换 stoken 有效：窗口是 IP 的属性。
                rotated = False
                if exit_strikes >= EXIT_STRIKE_LIMIT and len(usable) > 1:
                    print(f"  [出口] {cur_tag} 连续 {exit_strikes} 次撞墙，"
                          f"换出口而不是继续重算", flush=True)
                    await ctx.close()
                    ei += 1
                    ctx, page, cur_tag, cur_port = await new_ctx(ei)
                    if await warm_stoken(page, ctx, r["source_job_id"]):
                        log_requests(con, REQ_PER_RECOVER, outcome="warm")
                        since_warm = 0
                        window_now = FETCH_WINDOW
                        exit_strikes = 0
                        rotated = True
                        await asyncio.sleep(random.uniform(*REWARM_WAIT))
                        rec = await fetch_job(page, r["source_job_id"])
                        log_requests(con, REQ_PER_JOB,
                                     outcome="ok" if rec.get("ok") else "challenged")
                # 换出口已经重暖并重取过值了，别再走下面这条重暖路径 ——
                # 否则同一条岗位会连着花两次重算 + 两次 fetch（4 次请求），
                # 正是这类重复让 b4 那轮单条成本冲到 4.13 次。
                if not rotated and await warm_stoken(page, ctx, r["source_job_id"]):
                    log_requests(con, REQ_PER_RECOVER, outcome="rewarm")
                    since_warm = 0
                    # ⚠️ 重暖成功后不要立刻 fetch。实测（首轮 12 条）两次窗口耗尽
                    # 的重试**都又撞了挑战页**，两条因此记未判定 —— 而 goto 已经
                    # 拿到新 cookie 了。原因是新 stoken 需要一小会儿才对
                    # fetch 生效（导航本身也还在收尾）。等一拍再打，
                    # 否则每个窗口边界都要白费一条。
                    await asyncio.sleep(random.uniform(*REWARM_WAIT))
                    rec = await fetch_job(page, r["source_job_id"])
                    log_requests(con, REQ_PER_JOB,
                                 outcome="ok" if rec.get("ok") else "challenged")
                    # 还不行就再等久一点试最后一次：一条已经花掉的请求
                    # 比多等 8 秒贵得多（而且下一轮还要重花）。
                    if not rec.get("ok") and rec.get("reason") == "challenged":
                        await asyncio.sleep(random.uniform(7.0, 11.0))
                        rec = await fetch_job(page, r["source_job_id"])
                        log_requests(con, REQ_PER_JOB,
                                     outcome="ok" if rec.get("ok") else "challenged")

            result = is_closed_status(rec.get("job_status")) if rec.get("ok") else None
            # 身份校验：被重定向时判定会张冠李戴，而降级是**单向**的。
            # 校验不过就按未判定处理，绝不把别人的状态写到这条上。
            if result is not None and not same_job(rec.get("h1"), r["title"]):
                print(f"  [身份不符] 请求 {r['title'][:20]!r} 得到 "
                      f"{(rec.get('h1') or '')[:20]!r}，按未判定处理", flush=True)
                result = None
            text = rec.get("active_time") or "" if rec.get("ok") else ""
            done += 1

            if result is None:
                unknown += 1
                streak += 1
                print(f"[未判定] {r['title'][:24]} | {r['company_name']}", flush=True)
                # 连续失败说明出口被标记了，停下来救，而不是继续空转。
                # 诊断也要用详情页判据，首页码只够回答隧道通不通。
                if streak >= FAIL_STREAK:
                    await ctx.close()
                    verdict, note = exit_verdict(cur_port)
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
                            print("[RISK-STOP] 所有出口都不可用，本轮停止。已处理的结果都已落库。")
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
                # ⚠️ 关闭分支也要写 `updated_at`。原先只有活跃度分支写，
                # 于是被判关闭的行仍带着上一轮的时间戳 ——
                # 「这条是什么时候核实为关闭的」就无法回答，
                # 而这正是判断一条降级结论还新不新的唯一依据。
                con.execute(
                    "UPDATE jobs SET job_invalid=1, job_status_desc='职位已关闭', "
                    "retention_status='sample', updated_at=CURRENT_TIMESTAMP "
                    "WHERE canonical_id=? AND retention_status='active'",
                    (r["canonical_id"],))
                con.commit()
                closed += 1
                print(f"[关闭] {r['title'][:24]} | {r['company_name']}", flush=True)
            elif result is False and text:
                # `text` 现在是 `.boss-active-time` 容器的值（单个文案），
                # 不再是整页 innerText。`parse_active_status` 仍然适用（它做
                # 子串匹配且按长度降序，避免「本周活跃」被「周活跃」误吃），
                # 但它「取位置最靠前那条」的设计原本是为了在整页文本里躲开
                # 推荐位里**别人家**岗位的活跃度 —— 按容器取值后那个风险已经不存在。
                phrase, days = parse_active_status(text)
                if phrase:
                    # ⚠️ 用 SQLite 的 CURRENT_TIMESTAMP（UTC），**不要**
                    # `datetime.now()`。这一列的既有口径是 UTC：`storage.py` 全程
                    # 用 CURRENT_TIMESTAMP 写了 8048 行，而两个刷新脚本原先写本地时间
                    # （+8h）—— 同一列两个时区，61 行已被污染。
                    # 项目里已有一次同形态的坑（`glm_tags_updated_at` 本地时间硬编码
                    # 了 `Z` 后缀），教训是**跨字段比时间前先看实际值的格式**；
                    # 这次更进一步：**同一字段的多个写入方也必须统一口径**。
                    con.execute(
                        "UPDATE jobs SET recruiter_active_status=?, recruiter_online=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE canonical_id=?",
                        (phrase, 1 if phrase == "在线" else 0, r["canonical_id"]))
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
                # 越线后所有等待按倍数拉长（日线是降速线，见 over_quota_factor）
                if args.burst and i % args.burst == 0:
                    bp = random.uniform(*args.burst_pause) * slow_factor
                    print(f"[节奏] 长停顿 {bp/60:.1f} 分钟"
                          + (f"（降速 {slow_factor:.0f}x）" if slow_factor > 1 else ""),
                          flush=True)
                    await asyncio.sleep(bp)
                else:
                    await asyncio.sleep(
                        random.uniform(*args.gap) * slow_factor)

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
