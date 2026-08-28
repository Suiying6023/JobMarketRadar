"""探测岗位详情页能否直接从 Vue 组件实例读到 JD，绕开详情接口的配额。

思路来自参考项目 GeekGeekRun：它不解析 DOM、也不自己发 wapi 请求，
而是读页面里已经渲染好的 Vue 组件数据：
    document.querySelector('.job-detail-box').__vue__.data
拿到的是结构化对象，不受前端 class 改版和字体反爬影响。

关键待验证的假设：页面导航走的是「页面配额」，可能与 /wapi/zpgeek/job/detail.json
的累计配额（实测约 12-13 次/窗口）相互独立。如果成立，这条路才是突破口。

本脚本只导航 1 个岗位，不发任何详情接口请求，并把页面上所有挂了 __vue__ 的
节点连同其数据键名 dump 到 data/vue_probe/ 供离线分析（避免反复导航浪费配额）。

踩过的坑，这里都避开：
  - 必须复用已登录页签，新开 page + goto 会停在 about:blank
  - 不能用 wait_until='networkidle'，BOSS 详情页永不触发，30s 必超时
  - URL 带上列表响应里的 lid / securityId（duck 项目的做法），裸 URL 可能被打回
  - evaluate 撞上安全校验跳转会抛 Execution context was destroyed，需重试

用法: python scripts/probe_vue_detail.py [job_detail_url] [lid] [securityId]
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "vue_probe"

ORIGIN = "https://www.zhipin.com"

RISK_TEXTS = ("访问频繁", "安全校验", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")

# 已知的异常页（来自参考项目）。落到这些 URL 说明需要人工介入。
ABNORMAL_URLS = (
    "/web/user/safe/verify-slider",
    "/web/common/security-check.html",
    "/web/common/403.html",
    "/web/common/error.html",
)

# 扫描页面上所有挂了 __vue__ 的节点，报告选择器、数据键名和 JD 候选长度。
# 不直接取固定选择器，因为 GeekGeekRun 用的 .job-detail-box 是推荐流页面的结构，
# 我们走的是 /job_detail/ 独立页，结构可能不同——先看清楚再决定取哪个。
SCAN_JS = r"""() => {
    const out = {
        url: location.href,
        title: document.title,
        bodyTextLen: (document.body ? document.body.innerText.length : 0),
        hasVueGlobal: typeof window.Vue !== 'undefined',
        nodes: [],
        jdCandidates: [],
    };
    const seen = new Set();
    const all = document.querySelectorAll('*');
    for (const el of all) {
        const vue = el.__vue__;
        if (!vue) continue;
        // 用 class 组合做标识，够定位就行
        const sel = el.tagName.toLowerCase() +
            (el.className && typeof el.className === 'string'
                ? '.' + el.className.trim().split(/\s+/).join('.')
                : '');
        if (seen.has(sel)) continue;
        seen.add(sel);

        const entry = {selector: sel, keys: [], dataKeys: []};
        try {
            entry.keys = Object.keys(vue).filter(k => !k.startsWith('_') && !k.startsWith('$'));
        } catch (e) { entry.keys = ['<err>']; }
        try {
            if (vue.$data) entry.dataKeys = Object.keys(vue.$data);
        } catch (e) { entry.dataKeys = ['<err>']; }
        out.nodes.push(entry);

        // 在这个组件实例上找像 JD 的字符串字段
        const probe = (obj, path, depth) => {
            if (!obj || depth > 2) return;
            let ks;
            try { ks = Object.keys(obj); } catch (e) { return; }
            for (const k of ks) {
                if (k.startsWith('_') || k.startsWith('$')) continue;
                let v;
                try { v = obj[k]; } catch (e) { continue; }
                if (typeof v === 'string' && v.length > 80) {
                    out.jdCandidates.push({
                        selector: sel, path: path + '.' + k,
                        len: v.length, head: v.slice(0, 100),
                    });
                } else if (v && typeof v === 'object' && depth < 2) {
                    probe(v, path + '.' + k, depth + 1);
                }
            }
        };
        probe(vue, '__vue__', 0);
    }
    return out;
}"""


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    url = sys.argv[1]
    lid = sys.argv[2] if len(sys.argv) > 2 else ""
    security_id = sys.argv[3] if len(sys.argv) > 3 else ""

    params = {}
    if lid:
        params["lid"] = lid
    if security_id:
        params["securityId"] = security_id
    target = f"{url}?{urlencode(params)}" if params else url

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            log("没有已登录的 zhipin 页签，先在浏览器里打开 https://www.zhipin.com/")
            return
        log(f"复用页签: {page.url}")

        log(f"导航到: {target}")
        try:
            # domcontentloaded 而不是 networkidle：后者在 BOSS 详情页永不触发
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError as exc:
            log(f"导航失败: {exc}")
            return

        # SPA 还要渲染一会儿，给足时间但不靠 networkidle
        await page.wait_for_timeout(6000)
        log(f"当前 URL: {page.url}")

        if any(bad in (page.url or "") for bad in ABNORMAL_URLS):
            log("!! 落到异常页/验证页，需要你在浏览器里人工处理后再跑")
            return

        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError as exc:
            if "Execution context was destroyed" in str(exc):
                log("上下文被导航销毁，等待 5s 重试")
                await asyncio.sleep(5)
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            else:
                raise

        hit = next((t for t in RISK_TEXTS if t in text), None)
        if hit:
            log(f"!! 页面正文命中风控关键字「{hit}」，停止")
            (OUT_DIR / "risk_page.txt").write_text(text[:5000], encoding="utf-8")
            return
        log(f"页面正文长度: {len(text)}")

        info = await page.evaluate(SCAN_JS)

        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        dump = OUT_DIR / f"vue-scan-{stamp}.json"
        dump.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

        log(f"挂载 __vue__ 的节点数: {len(info['nodes'])}")
        for node in info["nodes"][:15]:
            log(f"  {node['selector'][:70]}")
            if node["dataKeys"]:
                log(f"      $data: {node['dataKeys'][:12]}")

        log(f"长文本候选数: {len(info['jdCandidates'])}")
        for cand in sorted(info["jdCandidates"], key=lambda c: -c["len"])[:8]:
            log(f"  len={cand['len']:5d} {cand['path'][:60]}")
            log(f"      {cand['head'][:80]!r}")

        log(f"完整结果: {dump}")


if __name__ == "__main__":
    asyncio.run(main())
