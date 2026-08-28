"""在搜索页点卡片进详情，验证 __vue__ 读取是否可行。

为什么不直接导航到 job_detail URL：2026-07-30 实测直接导航会被弹回首页
（当前 URL 变成 https://www.zhipin.com/，正文是首页内容）。参考项目
GeekGeekRun 和 get_jobs 都是在搜索页点卡片靠 SPA 内部路由进详情，不直接导航。

重要预期修正：点卡片时页面内部照样会请求 /wapi/zpgeek/job/detail.json
（get_jobs 就是靠 waitForResponse 截获它拿数据的），所以这条路**不能绕开
详情接口配额**。本探针同时挂响应监听，把这件事实测出来记下来。

用法: python scripts/probe_vue_click.py [关键词]
只点 1 张卡片就停，前台可见，便于人工过验证。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "vue_probe"

ORIGIN = "https://www.zhipin.com"
SEARCH_BASE = f"{ORIGIN}/web/geek/jobs"  # 复数，单数会 302
CITY_HANGZHOU = "101210100"

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")

# 参考项目验证过的选择器：ggr 用 ul.rec-job-list li.job-card-box（推荐流）,
# get_jobs 用 //ul[contains(@class,'rec-job-list')]。搜索页可能是 job-card-wrapper。
CARD_SELECTORS = (
    "li.job-card-box",
    "ul.rec-job-list li",
    "li.job-card-wrapper",
    ".job-list-box li",
)

# ggr 的做法：不解析 DOM，直接读 Vue 组件实例。
SCAN = r"""() => {
    const out = {url: location.href, nodes: [], vueGlobal: typeof window.Vue !== 'undefined'};
    const all = document.querySelectorAll('*');
    for (const el of all) {
        const vue = el.__vue__ || el.__vue_app__ || el.__vueParentComponent;
        if (!vue) continue;
        let keys = [];
        try {
            const target = el.__vue__ || (el.__vueParentComponent && el.__vueParentComponent.ctx);
            keys = target ? Object.keys(target).filter(k => !k.startsWith('_') && !k.startsWith('$')).slice(0, 40) : [];
        } catch (e) { keys = ['<err:' + String(e).slice(0, 40) + '>']; }
        out.nodes.push({
            tag: el.tagName.toLowerCase(),
            cls: (el.className || '').toString().slice(0, 90),
            kind: el.__vue__ ? 'vue2' : (el.__vueParentComponent ? 'vue3' : 'app'),
            keys: keys,
        });
        if (out.nodes.length >= 25) break;
    }
    // JD 文本候选：正文里最长的几个块，用来对照 __vue__ 是否拿得更干净
    const cands = [];
    for (const el of document.querySelectorAll('div,section')) {
        const t = (el.innerText || '').trim();
        if (t.length > 200 && el.children.length < 12) {
            cands.push({cls: (el.className || '').toString().slice(0, 70), len: t.length,
                        head: t.slice(0, 100)});
        }
    }
    cands.sort((a, b) => b.len - a.len);
    out.jdCandidates = cands.slice(0, 6);
    out.bodyLen = (document.body.innerText || '').length;
    return out;
}"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "AI Agent"
    OUT.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright
    from playwright.async_api import Error as PWError

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            log("没有已登录的 zhipin 页签，先在浏览器里打开首页")
            return
        log(f"复用页签: {page.url}")

        # 记录页面自己发出的接口请求，用来判定「点卡片是否消耗详情配额」
        seen: list[dict] = []
        risk_hit: list[str] = []

        async def on_response(resp):
            u = resp.url
            if "/wapi/" not in u:
                return
            short = u.split("/wapi/")[-1].split("?")[0]
            entry = {"api": short, "status": resp.status}
            seen.append(entry)
            log(f"  <- 页面请求 {short} HTTP {resp.status}")
            if "job/detail.json" in u:
                try:
                    txt = await resp.text()
                except Exception:
                    return
                hit = next((t for t in RISK_TEXTS if t in txt), None)
                if hit:
                    risk_hit.append(hit)
                    log(f"  !! detail.json 命中风控关键字「{hit}」")
                else:
                    try:
                        body = json.loads(txt or "{}")
                        jd = (((body.get("zpData") or {}).get("jobInfo") or {})
                              .get("postDescription") or "")
                        log(f"  detail.json code={body.get('code')} JD={len(jd)} 字符")
                    except json.JSONDecodeError:
                        pass

        page.on("response", on_response)

        params = {"query": keyword, "city": CITY_HANGZHOU}
        url = f"{SEARCH_BASE}?{urlencode(params)}"
        log(f"导航搜索页: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except PWError as exc:
            log(f"导航异常: {str(exc)[:120]}")
        await page.wait_for_timeout(6000)
        log(f"当前 URL: {page.url}")

        if ORIGIN not in (page.url or "") or "/web/geek/job" not in (page.url or ""):
            log("没停在搜索页，停止（不继续点击，避免浪费配额）")
            return

        # 找卡片
        cards = None
        used = ""
        for sel in CARD_SELECTORS:
            loc = page.locator(sel)
            n = await loc.count()
            log(f"选择器 {sel} -> {n} 个")
            if n > 0 and cards is None:
                cards, used = loc, sel
        if cards is None:
            log("没找到岗位卡片，停止")
            return

        total = await cards.count()
        log(f"用 {used}，共 {total} 张卡片。点第 1 张")

        before = len(seen)
        try:
            await cards.first.click(timeout=15000)
        except PWError as exc:
            log(f"点击失败: {str(exc)[:120]}")
            return
        await page.wait_for_timeout(6000)
        log(f"点击后 URL: {page.url}")
        log(f"点击触发了 {len(seen) - before} 个 /wapi/ 请求")

        if risk_hit:
            log(f"命中风控「{risk_hit[0]}」，停止")
            return

        scan = await page.evaluate(SCAN)
        log(f"挂载 Vue 的节点数: {len(scan.get('nodes') or [])}  正文 {scan.get('bodyLen')} 字符")
        for n in (scan.get("nodes") or [])[:12]:
            log(f"  [{n['kind']}] {n['tag']}.{n['cls'][:50]} keys={n['keys'][:10]}")
        for c in (scan.get("jdCandidates") or [])[:4]:
            log(f"  JD候选 {c['len']:5d} .{c['cls'][:45]} | {c['head'][:60]}")

        scan["apiCalls"] = seen
        path = OUT / f"vue-click-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"完整结果: {path}")


if __name__ == "__main__":
    asyncio.run(main())
