"""查 token / zp_token / traceid 的来源。

零网络请求：只读当前页面的 cookie、localStorage、sessionStorage 和
若干全局变量，不发 fetch、不导航。

背景：2026-07-30 监听人工操作，发现页面自己发的 detail.json 带了 4 个
我们手动 fetch 没带的头——token、zp_token、traceid、x-requested-with。
怀疑这才是节流的真正原因（缺凭证 -> 配额被压到 12-13 次）。

用法: python scripts/probe_token_source.py
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "token_probe"
ORIGIN = "https://www.zhipin.com"

# 监听到的真实值，用来在各存储里反查它们从哪来。
KNOWN = {
    "token": "8GJVqmbrjhi7vZ28",
    "zp_token": "V2Rt0jFOz73FltVtRuxxsaKC627DrTxi8~|Rt0jFOz73FltVtRuxxsaKC627DrXzS4~",
}

SCAN = r"""(known) => {
    const out = {url: location.href, cookies: {}, local: {}, session: {}, globals: {}, matches: []};

    for (const part of document.cookie.split(';')) {
        const i = part.indexOf('=');
        if (i < 0) continue;
        const k = part.slice(0, i).trim();
        const v = part.slice(i + 1).trim();
        out.cookies[k] = v.length > 120 ? v.slice(0, 120) + '...' : v;
    }
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            const v = localStorage.getItem(k) || '';
            out.local[k] = v.length > 160 ? v.slice(0, 160) + '...' : v;
        }
    } catch (e) { out.local['<err>'] = String(e); }
    try {
        for (let i = 0; i < sessionStorage.length; i++) {
            const k = sessionStorage.key(i);
            const v = sessionStorage.getItem(k) || '';
            out.session[k] = v.length > 160 ? v.slice(0, 160) + '...' : v;
        }
    } catch (e) { out.session['<err>'] = String(e); }

    // 常见的全局挂载点
    for (const name of ['__ZP__', '_ZP', 'ZP', '__NUXT__', '__INITIAL_STATE__', 'Vue']) {
        try {
            if (typeof window[name] !== 'undefined') {
                out.globals[name] = Object.keys(window[name] || {}).slice(0, 30);
            }
        } catch (e) { /* ignore */ }
    }

    // 反查：已知的 token 值出现在哪个存储的哪个键里
    const scanFor = (label, obj) => {
        for (const [k, v] of Object.entries(obj)) {
            for (const [name, want] of Object.entries(known)) {
                if (!want) continue;
                const head = want.split('|')[0].slice(0, 16);
                if (typeof v === 'string' && head && v.includes(head)) {
                    out.matches.push({where: label, key: k, wants: name});
                }
            }
        }
    };
    scanFor('cookie', out.cookies);
    scanFor('localStorage', out.local);
    scanFor('sessionStorage', out.session);
    return out;
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright
    from playwright.async_api import Error as PWError

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            print("没有已登录的 zhipin 页签")
            return
        print(f"页签: {page.url}\n")

        data = None
        for attempt in range(1, 4):
            try:
                data = await page.evaluate(SCAN, KNOWN)
                break
            except PWError as exc:
                if "Execution context was destroyed" not in str(exc) or attempt == 3:
                    raise
                print(f"上下文被销毁（页面在跳转），重试 {attempt}")
                await asyncio.sleep(4)

        print("--- cookie ---")
        for k, v in data["cookies"].items():
            print(f"  {k} = {v}")
        print("\n--- localStorage ---")
        for k, v in data["local"].items():
            print(f"  {k} = {v}")
        print("\n--- sessionStorage ---")
        for k, v in (data["session"] or {}).items():
            print(f"  {k} = {v}")
        print("\n--- 全局变量 ---")
        for k, v in (data["globals"] or {}).items():
            print(f"  window.{k}: {v}")

        print("\n--- 已知 token 值的来源反查 ---")
        if data["matches"]:
            for m in data["matches"]:
                print(f"  {m['wants']} <- {m['where']}[{m['key']}]")
        else:
            print("  没找到。说明 token 不是直接存的，可能是运行时计算或已轮换。")

        path = OUT / f"token-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果: {path}")


if __name__ == "__main__":
    asyncio.run(main())
