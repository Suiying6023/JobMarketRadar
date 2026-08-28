"""路径1探针：patchright launch_persistent_context 直接启动浏览器（不连调试端口）。

对比 connect_over_cdp：patchright 直接拉起 Chrome 进程，不附加 CDP 调试器、
不开调试端口，理论上绕过 BOSS 的端口扫描检测（boss-zhipin-scraper issue #30
反编译确认 CDP 检测核心是扫描 9222/9223/9229 端口）。

用法: python scripts/probe_patchright.py [--profile path] [--fresh]
  --profile  要用的 profile 目录（默认 .browser-profile-patchright 副本，避免锁冲突）
  --fresh    用全新临时 profile（不含登录态，只验证启动与不刷新）

说明：当前 .browser-profile 正被 9222 端口的 Chrome 占用，直接复用会撞 profile 锁。
所以默认复制一份出来用，或 --fresh 全新。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import time
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://www.zhipin.com"
DEFAULT_PROFILE = ROOT / ".browser-profile"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()

    if args.fresh:
        profile_dir = ROOT / ".browser-profile-patchright-fresh"
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
    else:
        profile_dir = Path(args.profile) if args.profile else (ROOT / ".browser-profile-patchright-copy")
        if not profile_dir.exists():
            # 复制当前登录 profile（含 cookie/登录态）
            print(f"复制登录 profile 到 {profile_dir} ...")
            shutil.copytree(DEFAULT_PROFILE, profile_dir, ignore=shutil.ignore_patterns(
                "*.pma", "Singleton*", "LOCK", "lockfile", "Cache", "GPUCache",
                "Code Cache", "DawnCache", "GrShaderCache", "ShaderCache",
            ))
        else:
            print(f"复用已有 profile: {profile_dir}")

    print(f"用 patchright launch_persistent_context 启动，profile: {profile_dir}")
    started = time.monotonic()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",          # 复用系统 Chrome 二进制
            headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print(f"启动耗时 {time.monotonic()-started:.1f}s，打开搜索页 ...")

        # 打开搜索页
        await page.goto(
            "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(4000)

        url = page.url
        print(f"页面地址: {url[:90]}")
        if "_security_check" in url:
            print("!! 触发安全校验跳转")

        # 等几秒观察是否刷新循环（连 CDP 时这里会疯狂 reload）
        urls = set()
        for _ in range(4):
            try:
                urls.add(page.url)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
        print(f"4.5s 内观察到的页面 URL 集合（若只有 1 个 = 无刷新循环）:")
        for u in sorted(urls):
            print(f"  - {u[:80]}")

        # 读登录态
        try:
            info = await page.evaluate("""() => {
                const p = window._PAGE || {};
                return {
                    hasPage: typeof window._PAGE !== 'undefined',
                    token: (p.token || '').slice(0, 24),
                    userId: (p.userId || '').toString().slice(0, 12),
                };
            }""")
            print(f"登录态: {info}")
        except Exception as exc:
            print(f"读登录态失败: {exc}")

        await page.wait_for_timeout(2000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
