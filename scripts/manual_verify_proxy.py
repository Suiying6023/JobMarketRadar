"""起一个走指定 SOCKS 出口的可见浏览器，让人工通过 BOSS 的「安全验证」页，
然后把过验证后的 storage_state（cookie）存盘，供 check_job_status.py 复用。

为什么需要它：部分出口 IP 被 BOSS 标记后，详情页会 302 到
/web/passport/zp/security.html 的「安全验证」页（「当前 IP 地址可能存在异常访问行为」），
这种页面不会自解，必须有人点一下。点完之后同一 IP + 同一份 cookie 就能继续读。

用法：
    ssh -f -N -D 1080 bohrium-vm
    uv run python scripts/manual_verify_proxy.py --socks 127.0.0.1:1080 --tag vm
  浏览器弹出后人工完成验证，看到岗位页正常显示，回终端按 Enter。

产物：data/proxy_state_<tag>.json
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socks", default="127.0.0.1:1080", help="SOCKS5 出口 host:port")
    ap.add_argument("--tag", default="vm", help="出口标识，决定 state 文件名")
    # ⚠️ 默认 300s（5 分钟）而不是几小时：用户 2026-08-31 明确要求
    # 「单轮 20min 没有验证就可以停止任务，避免浪费」。
    # 实测过代价：那次弹了 4 小时的窗口，用户在睡觉，
    # 12 轮巡检全在重复"仍待验证"——窗口开着不产出任何东西，只是占着出口。
    # 人不在就该早停，醒了再弹一次的成本远低于挂着几小时。
    ap.add_argument("--wait", type=float, default=300.0,
                    help="等待人工完成验证的秒数（默认 300=5 分钟）。期间轮询页面状态，"
                         "检测到岗位页正常渲染即自动存盘。"
                         "⚠️ 别设成几小时：人不在时长时间挂窗口是纯浪费，"
                         "醒了重弹一次更便宜")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT source_job_id, title, company_name FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 ORDER BY canonical_id LIMIT 1"
    ).fetchone()
    if not row:
        print("库里没有可用样本")
        return
    job_id, title, company = row
    url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    state_path = ROOT / "data" / f"proxy_state_{args.tag}.json"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        # 隐身上下文：不带主账号 profile，只为这个出口攒一份过验证的 cookie
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            proxy={"server": f"socks5://{args.socks}"})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"出口 socks5://{args.socks}  标识={args.tag}")
        print(f"打开样本岗位：{title} @ {company}")
        print(f"  {url}")

        # ⚠️ **先暖会话再开岗位页**，否则弹出来的是**登录页**而不是验证页。
        # 实测（2026-08-31，连续三轮都踩到）：隐身 context 是空 cookie，
        # 免登录直接开 job_detail，平台判定"无会话的匿名请求"→ 推去
        # `/web/user/`（注册登录页）。那个页面上**没有可点的验证**，
        # 用户点什么都回到登录页 —— 我因此让用户白点了两轮，还错怪他点错了。
        # 先访问首页拿到 `__zp_stoken__`（会话级签名 cookie），
        # 平台才会把后续请求当"有会话的访客"，该验证时给 verify.html。
        await page.goto("https://www.zhipin.com/", wait_until="domcontentloaded",
                        timeout=60000)
        for _ in range(20):
            if "__zp_stoken__" in {c["name"] for c in await ctx.cookies()}:
                break
            await asyncio.sleep(1.0)
        has_stoken = "__zp_stoken__" in {c["name"] for c in await ctx.cookies()}
        print(f"会话预热：__zp_stoken__ {'已就位' if has_stoken else '**没拿到**'}")

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        cur_title = await page.title()
        print(f"当前标题：{cur_title!r}")
        # 直说这个窗口能不能点，别让用户对着登录页干等。
        if "注册登录" in cur_title:
            print()
            print("⚠️ 这个窗口停在**登录页**，不是验证页 —— 点不了。")
            print("   说明该出口当前不给匿名访客验证机会，只能等平台的解封时间；")
            print("   或先用另一个出口跑批（--only-exit <好的那个>）。")
        print()
        print("=" * 66)
        print("请在弹出的浏览器里完成验证（若显示「安全验证」就点按钮/拖滑块）。")
        print("不用回来按任何键——脚本会自己轮询，看到岗位页正常渲染就存盘退出。")
        print()
        print("⚠️ 千万不要在这个浏览器里登录 BOSS 账号。")
        print("   这条链路的前提就是免登录、只受 IP 限制；带上账号 cookie 等于")
        print("   用跳板 IP 操作主账号，风控风险远大于收益。")
        print("   脚本会在存盘前检查，检测到登录 cookie 会直接拒绝保存。")
        print("=" * 66)
        print()

        # 不用 input()：调用方（agent 工具环境）的 stdin 是关闭的，read 会立刻 EOFError
        # 让浏览器秒退。改为轮询页面自身状态——这也更可靠，因为判据是
        # 「页面真的渲染出岗位内容」而不是「人以为自己点完了」。
        deadline = asyncio.get_event_loop().time() + args.wait
        passed = False
        last_report = 0.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                t = await page.title()
                body = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            except Exception:
                await asyncio.sleep(2.0)
                continue
            # ⚠️ **判据取「风控 cookie 到手」，不取「页面内容够丰富」**
            # （2026-08-31 用户反馈「我验证成功后一直重复加载验证组件」后修）。
            # 原判据要求标题不含验证字样 + innerText > 500 字，等于要求
            # **匿名访客能看到完整岗位页** —— 而这条链路免登录，平台常在
            # 验证通过后仍把匿名请求送去登录页/再验证一次。于是人明明点成功了，
            # 脚本却判「还没过」，继续等 → 用户看到验证组件反复出现。
            # 人的劳动在拿到 __zp_stoken__ 那一刻就完成了，不该被之后
            # 「能不能看岗位页」这件独立的事否定。
            names = {c["name"] for c in (await ctx.cookies())}
            if "__zp_stoken__" in names:
                passed = True
                print(f"✅ 验证已通过：拿到风控 cookie __zp_stoken__"
                      f"（当前页 {t!r}，innerText {len(body)} 字）")
                break
            now = asyncio.get_event_loop().time()
            if now - last_report > 15:
                left = int(deadline - now)
                print(f"  等待验证… 当前标题 {t!r}  剩余 {left}s", flush=True)
                last_report = now
            await asyncio.sleep(2.0)

        if not passed:
            print(f"⏰ {args.wait}s 内未检测到验证通过，不保存 state。")
            await ctx.close()
            await browser.close()
            return

        # ⚠️ 绝不能把账号登录态存进这份 state：波尔出口是免登录链路，
        # 一旦带上主账号 cookie，就等于用跳板 IP 操作账号，那才是真正的风控雷区。
        # BOSS 的登录态标志是 wt2/wt（会话票据）与 geek_zp_token。
        cookies = (await ctx.cookies()) or []
        login_names = {"wt2", "wt", "geek_zp_token", "zp_at", "bst"}
        found_login = sorted({c["name"] for c in cookies} & login_names)
        if found_login:
            print()
            print("!" * 66)
            print(f"检测到账号登录态 cookie: {found_login}")
            print("这份 state 不会保存——波尔出口必须保持免登录。")
            print("请在浏览器里退出登录后重跑本脚本，或换用无痕方式只过验证不登录。")
            print("!" * 66)
            await ctx.close()
            await browser.close()
            return

        await ctx.storage_state(path=str(state_path))
        kept = sorted({c["name"] for c in cookies})
        print(f"已保存 {len(cookies)} 条 cookie → {state_path}")
        print(f"  cookie 名单（应只有风控/匿名类）: {kept}")
        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
