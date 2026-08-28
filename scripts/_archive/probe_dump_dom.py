"""把当前页签的 DOM 结构 dump 出来，用于确定真实选择器。

零接口请求：只读已经加载好的页面，不发 fetch、不导航。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from playwright.async_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "vue_probe"
ORIGIN = "https://www.zhipin.com"

SCAN = r"""() => {
    const out = {
        url: location.href,
        title: document.title,
        bodyTextLen: document.body ? document.body.innerText.length : 0,
        bodyTextHead: document.body ? document.body.innerText.slice(0, 600) : '',
    };

    // 页面上出现频率最高的 class（取前 40），用来反推列表容器
    const freq = {};
    for (const el of document.querySelectorAll('*')) {
        for (const c of (el.classList || [])) {
            freq[c] = (freq[c] || 0) + 1;
        }
    }
    out.topClasses = Object.entries(freq)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 40)
        .map(([c, n]) => c + ' x' + n);

    // 所有 li 的 class 组合分布
    const liFreq = {};
    for (const li of document.querySelectorAll('li')) {
        const key = li.className || '(no-class)';
        liFreq[key] = (liFreq[key] || 0) + 1;
    }
    out.liClasses = Object.entries(liFreq).sort((a, b) => b[1] - a[1]).slice(0, 25);

    // 任何 class 里含 job / card / list 的元素
    const hits = [];
    for (const el of document.querySelectorAll('[class]')) {
        const cn = String(el.className || '');
        if (/job|card|list|position/i.test(cn)) {
            hits.push({
                tag: el.tagName.toLowerCase(),
                cls: cn.slice(0, 80),
                textLen: (el.innerText || '').length,
            });
        }
    }
    out.jobishCount = hits.length;
    out.jobish = hits.filter(h => h.textLen > 20).slice(0, 40);

    // __vue__ 挂载点
    const vue = [];
    for (const el of document.querySelectorAll('*')) {
        if (el.__vue__) {
            const keys = [];
            try {
                for (const k in el.__vue__) {
                    if (!k.startsWith('_') && !k.startsWith('$')) keys.push(k);
                }
            } catch (e) { /* ignore */ }
            vue.push({
                tag: el.tagName.toLowerCase(),
                cls: String(el.className || '').slice(0, 70),
                keys: keys.slice(0, 30),
            });
        }
    }
    out.vueNodes = vue.slice(0, 25);
    out.vueCount = vue.length;
    out.hasVue3 = !!(document.querySelector('#wrap') || {}).__vue_app__
                  || !!document.querySelector('[data-v-app]');

    // 是否是异常页
    const t = document.body ? document.body.innerText : '';
    out.markers = {
        needLogin: /登录|扫码/.test(t),
        verify: /验证|滑块|安全校验/.test(t),
        frequent: /访问频繁|操作过于频繁/.test(t),
        noResult: /暂无|没有找到|无结果/.test(t),
    };
    return out;
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if ORIGIN in (p.url or "")), None)
        if page is None:
            print("没有 zhipin 页签")
            return

        data = None
        for attempt in range(1, 4):
            try:
                data = await page.evaluate(SCAN)
                break
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc) or attempt == 3:
                    raise
                print(f"上下文被销毁，重试 {attempt}")
                await asyncio.sleep(4)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = OUT / f"dom-{stamp}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"URL: {data['url']}")
        print(f"title: {data['title']}")
        print(f"正文长度: {data['bodyTextLen']}")
        print(f"异常标记: {data['markers']}")
        print(f"__vue__ 节点数: {data['vueCount']}  Vue3 迹象: {data['hasVue3']}")
        print(f"class 含 job/card/list 的元素: {data['jobishCount']}")
        print("\n--- 正文开头 ---")
        print(data['bodyTextHead'][:400])
        print("\n--- 高频 class ---")
        print(", ".join(data['topClasses'][:20]))
        print("\n--- li class 分布 ---")
        for cls, n in data['liClasses'][:12]:
            print(f"  {n:4d}  {cls[:70]}")
        print("\n--- jobish 元素（有文本的）---")
        for h in data['jobish'][:15]:
            print(f"  <{h['tag']}> len={h['textLen']:6d}  {h['cls'][:60]}")
        print("\n--- __vue__ 节点 ---")
        for v in data['vueNodes'][:10]:
            print(f"  <{v['tag']}> {v['cls'][:40]}")
            print(f"      keys: {', '.join(v['keys'][:12])}")
        print(f"\n完整结果: {path}")


if __name__ == "__main__":
    asyncio.run(main())
