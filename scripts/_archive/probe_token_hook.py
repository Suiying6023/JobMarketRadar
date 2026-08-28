"""定位 token 头的来源。

已知：页面自己发的 detail.json 带 token: 8GJVqmbrjhi7vZ28（16位字母数字），
但 cookie/localStorage/sessionStorage/常见全局里都没有。

本脚本做两件事（都零网络请求）：
  1. 扫描 window / document 上所有可能藏 token 的属性（启发式）
  2. 挂钩 fetch 和 XHR 的 setRequestHeader，被动观察页面 JS 给请求设了哪些头、
     以及在哪个调用栈里设置的——重点看 token 值是不是每次请求从某个源取的。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "token_probe"
ORIGIN = "https://www.zhipin.com"

# 探针脚本，注入到页面上下文。只做读取和监听，不主动发请求。
# 为了触发页面自己的请求（好观察它怎么加 token），页面加载后允许 30 秒，
# 这期间如果用户手动点一下搜索，就能看到真实的请求头发送流程。
SCAN = r"""() => {
    const out = {found: [], hook_log: [], url: location.href};

    // 1) 全局里启发式找像 token 的值
    const candidates = {};
    const push = (label, obj) => {
        try {
            for (const k of Object.keys(obj || {})) {
                if (k.toLowerCase().includes('token')) {
                    const v = obj[k];
                    if (typeof v === 'string' && v && v.length < 200) {
                        out.found.push({label, key: k, value: v});
                    }
                }
            }
        } catch (e) { /* ignore */ }
    };
    push('window', window);
    if (document.documentElement) push('html', document.documentElement);
    if (document.head) push('head', document.head);
    for (const key of ['__ZP__','_ZP','ZP','__zhipin','zhipin','tokenStore','config','appConfig']) {
        try { push('window.' + key, window[key]); } catch (e) {}
    }

    // 2) 挂钩 fetch，记录请求头里出现 token 的调用栈片段
    const origFetch = window.fetch;
    window.__tokenHooked = true;
    window.fetch = async function (...args) {
        const [input, init] = args;
        try {
            const h = (init && init.headers) || (input && input.headers) || {};
            if (typeof h !== 'object') return origFetch.apply(this, args);
            const merged = {...h};
            // 尝试展开 Headers 实例
            if (typeof Headers !== 'undefined') {
                try {
                    const hd = new Headers(h);
                    hd.forEach((v, k) => { merged[k] = v; });
                } catch (e) {}
            }
            if (merged['token'] || merged.token) {
                out.hook_log.push({
                    kind: 'fetch',
                    url: String(input || '').slice(0, 150),
                    token: merged['token'] || merged.token,
                    stack: (new Error().stack || '').split('\n').slice(1, 6).join(' | '),
                });
            }
        } catch (e) {}
        return origFetch.apply(this, args);
    };
    return out;
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            print("没有 zhipin 页签")
            return
        print(f"页签: {page.url}")

        # 注入挂钩 + 扫描
        data = await page.evaluate(SCAN)

        print(f"\n--- 启发式找到的 token 值 ---")
        for f in data["found"]:
            print(f"  {f['label']}.{f['key']} = {f['value']}")

        print(f"\n--- 挂钩是否就位 ---")
        print(f"  window.fetch 已替换: {await page.evaluate('window.__tokenHooked === true')}")
        print(f"  已捕获 {len(data['hook_log'])} 次带 token 的 fetch")

        # 等 25 秒，给用户时间在页面上做一次正常搜索，观察页面请求头
        print(f"\n  等待 25 秒…（期间请在页面上随便点一次搜索，观察请求头）")
        for i in range(5):
            await asyncio.sleep(5)
            log = await page.evaluate("() => window.__hook_log || []")
            print(f"  [{i*5+5}s] 累计捕获 {len(log)} 次")
            if log:
                print("  --- 最近一条 ---")
                print(f"    url: {log[-1]['url']}")
                print(f"    token: {log[-1]['token']}")
                print(f"    stack: {log[-1]['stack']}")

        # 收集最终结果
        final = await page.evaluate("() => ({log: window.__hook_log || [], url: location.href})")
        path = OUT / f"token-hook-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        payload = {"scan": data["found"], "hooks": final["log"]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果: {path}")


if __name__ == "__main__":
    asyncio.run(main())
