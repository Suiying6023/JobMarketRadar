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
    ap.add_argument("--wait", type=float, default=300.0,
                    help="等待人工完成验证的秒数（默认 300）。期间轮询页面状态，"
                         "检测到岗位页正常渲染即自动存盘")
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
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"当前标题：{await page.title()!r}")
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
            if "验证" not in t and "请稍候" not in t and t.strip() and len(body) > 500:
                passed = True
                print(f"✅ 验证已通过：标题 {t!r}，innerText {len(body)} 字")
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
    asyncio.run(main())
