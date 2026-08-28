"""popomv.me：全量枚举所有信息接口（含 GET/POST 方法），按类别整理。零服务器压力。"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from patchright.async_api import async_playwright

URL = "https://popomv.me/"
PROFILE = Path(".tmp-popomv-profile")


async def main() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=False, no_viewport=True,
        args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
        ignore_default_args=["--enable-automation"],
    )
    page = await ctx.new_page()
    try:
        await page.goto(URL, wait_until="networkidle", timeout=45000)
        scripts = await page.evaluate(
            """async () => { const o=[]; for (const s of document.scripts)
                if (s.src && s.src.startsWith('http') && !/baidu|hm\.|gtimg|analytics/.test(s.src))
                    try { o.push({url:s.src, txt:await (await fetch(s.src)).text()}); } catch(e){} return o; }"""
        )
        all_js = "\n".join(s["txt"] for s in scripts)
        print(f"脚本数: {len(scripts)} | 总字符: {len(all_js)}", flush=True)

        # 提取所有 /api/v1/... 路径（引号 + 模板字符串）
        paths = set()
        for m in re.finditer(r'/api/v1/[a-zA-Z0-9_\./$\-]+', all_js):
            p = m.group(0).rstrip(".")
            # 去掉模板变量残留
            p = re.sub(r'\$\{[^}]+\}', '{VAR}', p)
            p = p.split('?')[0]
            if p.count('/') >= 3 and '{VAR}' not in p:
                paths.add(p)

        # 判断方法：找 fetch("...", {method:"POST"}) 或 POST 关键字附近
        method_map = {}
        for m in re.finditer(r'(?:fetch|axios)\([^)]{0,10}?["\'`]([^"\'`]{10,120})["\'`].{0,120}?method\s*[:=]\s*["\'](GET|POST|PUT|DELETE)', all_js, re.S):
            url, method = m.group(1), m.group(2)
            method_map[url] = method

        # 分组
        groups = {}
        for p in sorted(paths):
            parts = p.split('/')
            # /api/v1/<domain>/<sub>/<file>.php
            if len(parts) >= 5:
                key = f"{parts[3]}/{parts[4]}" if len(parts) >= 5 else parts[3]
                key = key if parts[4] != 'company_detail' or len(parts) < 6 else f"company_detail/{parts[5]}"
            else:
                key = parts[3] if len(parts) > 3 else 'other'
            groups.setdefault(key, []).append(p)

        for g in sorted(groups):
            print(f"\n=== {g} ===", flush=True)
            for p in groups[g]:
                # 找该 URL 的方法（用 URL 子串匹配）
                meth = next((m for u, m in method_map.items() if u.split('?')[0].endswith(p.split('/')[-1])), "GET/POST")
                print(f"  {meth:8} {p}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await ctx.close()
    await pw.stop()
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
