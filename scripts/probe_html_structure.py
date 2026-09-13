"""摸清 job_detail HTML 的结构，找出判定字段的**精确选择器**。

**为什么必须做这一步**：`probe_html_fetch.py` 证明同源 fetch 能 1 次请求拿到整页
HTML，但它用「整篇文本里找子串」判定，结果 5 条里 4 条报「职位已关闭」，
而库里都是在招 —— 因为 fetch 回来的 HTML **没有 CSS**，那些本该 `display:none`
的模板节点、以及「更多职位」区块里**别人家岗位**的文案，全都混进了纯文本。

同理 `parse_active_status` 取「位置最靠前的那条」，靠的是**视觉顺序**；
在原始 HTML 里顺序可能完全不同，取到的可能是推荐位里另一个岗位的活跃度。

所以要在 HTML 里定位：
  ① 当前岗位的关闭状态到底写在哪个节点（还是根本不在 HTML 里，只由 JS 渲染）
  ② 当前岗位招聘者的活跃度文案在哪个节点
  ③ 有没有内嵌的 JSON 状态（`__INITIAL_STATE__` 这类），那比抠 DOM 稳得多

做法：fetch 一条 HTML → 用页面内 `DOMParser` 解析成真正的 DOM → 打出候选节点的
选择器路径与文本。**用 DOMParser 而不是正则**，因为要的就是「结构」这个信息。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import CLOSED_MARKERS, ACTIVE_DAYS  # noqa: E402

BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
               "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")

# 在页面里 fetch HTML，再用 DOMParser 解析，报告命中节点的结构位置。
# 关键：对每个命中文案，回溯出 ancestor 链（tag.class），这样才能判断它属于
# 「当前岗位」还是「更多职位」推荐位。
ANALYZE = """async ({url, closedMarkers, activePhrases}) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    if (html.length < 60000) {
        return {challenged: true, htmlLen: html.length, status: r.status};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');

    const pathOf = (el) => {
        const parts = [];
        let n = el;
        while (n && n.nodeType === 1 && parts.length < 7) {
            let s = n.tagName.toLowerCase();
            const cls = (n.getAttribute('class') || '').trim().split(/\\s+/)
                          .filter(Boolean).slice(0, 3);
            if (cls.length) s += '.' + cls.join('.');
            parts.unshift(s);
            n = n.parentElement;
        }
        return parts.join(' > ');
    };

    // 找含指定文案的**最深**节点（避免报到 body 上）
    const findText = (needles) => {
        const out = [];
        const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const t = (walker.currentNode.nodeValue || '').trim();
            if (!t) continue;
            for (const m of needles) {
                if (t.includes(m)) {
                    const el = walker.currentNode.parentElement;
                    out.push({marker: m, text: t.slice(0, 40), path: pathOf(el),
                              hidden: !!(el.closest('[style*="display:none"],[style*="display: none"]'))});
                    break;
                }
            }
        }
        return out;
    };

    // 内嵌 JSON 状态：多数 SSR 框架会塞一个全局对象
    const inlineJson = [];
    for (const s of doc.querySelectorAll('script')) {
        const txt = s.textContent || '';
        for (const key of ['__INITIAL_STATE__', '__NUXT__', '_PAGE',
                           'window.__DATA__', 'jobStatus', 'invalidStatus']) {
            if (txt.includes(key)) {
                const at = txt.indexOf(key);
                inlineJson.push({key, sample: txt.slice(at, at + 220)});
            }
        }
    }

    // 关键容器是否存在（判断是 SSR 还是纯 SPA 壳）
    const probes = {};
    for (const sel of ['.job-banner', '.job-detail', '.info-primary',
                       '.job-sec-text', '.job-detail-section',
                       '.job-boss-info', '.boss-info-attr', '.job-status',
                       '.job-limit', '.gray-text', '.sider-company',
                       '.job-detail-box', '.job-primary', '.error-content',
                       '.job-list-box', '.look-more', '.recommend-job-list']) {
        const els = doc.querySelectorAll(sel);
        if (els.length) probes[sel] = {n: els.length,
                                       first: (els[0].innerText || els[0].textContent || '').trim().slice(0, 120)};
    }

    return {
        challenged: false, status: r.status, htmlLen: html.length,
        title: (doc.querySelector('title') || {}).textContent || '',
        closedHits: findText(closedMarkers),
        activeHits: findText(activePhrases),
        inlineJson, probes,
    };
}"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--save-html", default="data/calibration/sample_job.html")
    ap.add_argument("--out", default="data/calibration/html-structure.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc != '职位已关闭' AND job_description != '' "
        "ORDER BY CASE WHEN detail_fetched_at IS NULL OR detail_fetched_at='' "
        "         THEN 0 ELSE 1 END, detail_fetched_at ASC LIMIT ?",
        (args.jobs,)).fetchall()
    con.close()

    sent: list[str] = []
    out_all = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def handler(route):
            req = route.request
            h = (req.url.split("/")[2] if "//" in req.url else "").lower()
            if req.resource_type in BLOCK_TYPES or any(b in h for b in BLOCK_HOSTS):
                await route.abort()
            else:
                sent.append(req.url)
                await route.continue_()
        await page.route("**/*", handler)

        # 冷启动拿会话 stoken
        url0 = f"https://www.zhipin.com/job_detail/{rows[0]['source_job_id']}.html"
        try:
            await page.goto(url0, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        start = loop.time()
        while loop.time() - start < 45:
            try:
                t = await page.title()
            except Exception:
                await asyncio.sleep(1.0); continue
            if t.strip() and "招聘" in t and "注册登录" not in t:
                break
            await asyncio.sleep(1.5)
        # 整页的可见判定，作为结构分析的对照
        vis = await page.evaluate(
            "() => document.body ? document.body.innerText : ''")
        print(f"[冷启动] 可见正文 {len(vis)} 字")
        for m in CLOSED_MARKERS:
            if m in vis:
                idx = vis.index(m)
                print(f"  ⚠️ 可见文本含「{m}」，上下文: "
                      f"{vis[max(0,idx-60):idx+30]!r}")
        print()

        for i, r in enumerate(rows, 1):
            url = f"https://www.zhipin.com/job_detail/{r['source_job_id']}.html"
            res = await page.evaluate(ANALYZE, {
                "url": url,
                "closedMarkers": list(CLOSED_MARKERS),
                "activePhrases": list(ACTIVE_DAYS.keys()),
            })
            res["job_id"] = r["source_job_id"]
            res["title"] = r["title"]
            out_all.append(res)
            print(f"[{i}] {r['title'][:26]}  html={res.get('htmlLen')} "
                  f"challenged={res.get('challenged')}")
            if res.get("challenged"):
                continue
            print(f"    title: {res['title'][:0]}{res.get('title','')[:50]!r}")
            print(f"    「已关闭」类命中 {len(res['closedHits'])} 处:")
            for h in res["closedHits"][:8]:
                print(f"      · {h['marker']} hidden={h['hidden']}")
                print(f"        {h['path']}")
            print(f"    活跃度命中 {len(res['activeHits'])} 处（库存 "
                  f"{r['recruiter_active_status']}）:")
            for h in res["activeHits"][:8]:
                print(f"      · {h['text']!r}")
                print(f"        {h['path']}")
            print(f"    容器探测: {json.dumps(res['probes'], ensure_ascii=False)[:600]}")
            if res["inlineJson"]:
                print(f"    内嵌 JSON: {[j['key'] for j in res['inlineJson']]}")
                for j in res["inlineJson"][:3]:
                    print(f"      {j['key']}: {j['sample'][:160]!r}")
            print(flush=True)
            if i == 1 and args.save_html:
                html = await page.evaluate(
                    "async ({url}) => (await fetch(url, {credentials:'include'})).text()",
                    {"url": url})
                p = ROOT / args.save_html
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(html, encoding="utf-8")
                print(f"    （HTML 已存 {p}，{len(html)} 字节）\n", flush=True)
            await asyncio.sleep(6.0)

        await ctx.close()
        await browser.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(out_all, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"结果已存 {out}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
