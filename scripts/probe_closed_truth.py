"""判定「关闭状态」到底能不能从匿名详情页读出来 —— 用**平台自己保证在招**的对照组。

`probe_fetch_window.py` 暴露的问题：我的 `.job-status` 判据对 24 条**全部**判「关闭」，
包括昨天刚采的 10 条。两侧样本一起看才发现它是个恒真判据 ——
只测「已知关闭」那一侧的话，它会得到 10/10 的满分。

**但还剩一个更要紧的岔路**，它决定的不是我的解析对不对，而是**现有生产脚本一直在做什么**：
  假设 A：这些岗位真的都关闭了（库里快照过期，`check_job_status._is_closed`
          按整篇文本找子串，同样会判关闭 —— 那么现有脚本的判定是**对的**）
  假设 B：匿名访问详情页时平台一律渲染「职位已关闭」（诱导登录），
          与岗位真实状态无关 —— 那么现有脚本一直在把**在招岗位误标成关闭并降级**，
          而降级是**单向**的，等于持续污染库

区分两个假设需要一组「平台自己保证在招」的岗位。BOSS 的城市 SEO 页
（`/hangzhou/`）列的就是当前在招岗位 —— 免登录、一次 curl 就能拿到上百个 id，
不消耗任何账号配额。**这些 id 若也被判「关闭」，就证明是假设 B。**

⚠️ 免登录。对照组岗位不入库，只用于验证判据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "scripts"))
from check_job_status import _is_closed, ACTIVE_DAYS  # noqa: E402

BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
               "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")

# 同时取三种判据，看它们在对照组上是否分道扬镳：
#   jobStatus   —— 我用的 `.job-status` 容器文本
#   fullText    —— 现有生产脚本用的「整篇纯文本找子串」（_is_closed）
#   visibleLike —— 只取岗位主区块的文本，排除登录浮层与推荐位
EXTRACT = """async ({url}) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    if (html.length < 60000) {
        return {ok: false, status: r.status, len: html.length};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const txt = (sel) => {
        const el = doc.querySelector(sel);
        return el ? (el.textContent || '').trim() : null;
    };
    const main = doc.querySelector('#main') || doc.body;
    return {
        ok: true, status: r.status, len: html.length,
        h1: txt('.info-primary .name h1') || txt('h1'),
        jobStatus: txt('.job-banner .job-status'),
        jobStatus2: txt('.smallbanner .job-status'),
        activeTime: txt('.boss-active-time'),
        salary: txt('.info-primary .salary'),
        jdLen: (txt('.job-sec-text') || '').length,
        // 整篇文本（生产脚本的口径）
        fullText: (doc.body.textContent || ''),
        mainText: (main.textContent || ''),
    };
}"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", type=int, default=8,
                    help="从 SEO 页取几条「平台保证在招」的对照组")
    ap.add_argument("--known-closed", type=int, default=4,
                    help="库里已知关闭的取几条")
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--seo", default="data/calibration/seo_list.html")
    ap.add_argument("--out", default="data/calibration/closed-truth.json")
    args = ap.parse_args()

    seo = (ROOT / args.seo).read_text(encoding="utf-8", errors="replace")
    control_ids = list(dict.fromkeys(
        re.findall(r"/job_detail/([A-Za-z0-9~_-]{20,40})\.html", seo)))[: args.control]
    print(f"对照组（SEO 在招页）{len(control_ids)} 条")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    closed_rows = con.execute(
        "SELECT source_job_id, title FROM jobs WHERE job_status_desc='职位已关闭' "
        "ORDER BY detail_fetched_at DESC LIMIT ?", (args.known_closed,)).fetchall()
    con.close()
    print(f"已知关闭 {len(closed_rows)} 条\n")

    targets = ([{"id": i, "label": "SEO在招", "title": ""} for i in control_ids]
               + [{"id": r["source_job_id"], "label": "库内已关闭",
                   "title": r["title"]} for r in closed_rows])

    sent: list[str] = []
    out = []

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

        async def cold(job_id: str) -> int:
            before = len(sent)
            try:
                await page.goto(f"https://www.zhipin.com/job_detail/{job_id}.html",
                                wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            while loop.time() - t0 < 60:
                if "__zp_stoken__" in {c["name"] for c in await ctx.cookies()}:
                    break
                await asyncio.sleep(1.0)
            return len(sent) - before

        # ⚠️ 冷启动用对照组的第一条：它落地时的**可见文本**是最硬的证据 ——
        # 真浏览器带 CSS 渲染，看到什么就是用户看到什么。
        c0 = await cold(targets[0]["id"])
        vis = await page.evaluate(
            "() => document.body ? document.body.innerText : ''")
        print(f"[冷启动] 对照组第 1 条整页加载 {c0} 次请求，可见文本 {len(vis)} 字")
        print(f"  可见文本含「职位已关闭」= {'职位已关闭' in vis}")
        print(f"  可见文本开头: {vis[:160]!r}\n", flush=True)

        for i, t in enumerate(targets, 1):
            url = f"https://www.zhipin.com/job_detail/{t['id']}.html"
            before = len(sent)
            # 恢复后紧接着 evaluate 会撞上「Execution context was destroyed」——
            # security-check/整页导航还没停稳。采集链路早有同一处教训
            # （boss.py:_evaluate_fetch 的 PlaywrightError 分支），这里同样要容错，
            # 否则一次导航竞态会把整轮探测打断。
            async def grab() -> dict:
                for _ in range(3):
                    try:
                        return await page.evaluate(EXTRACT, {"url": url})
                    except Exception as exc:
                        if "Execution context was destroyed" not in str(exc):
                            raise
                        await asyncio.sleep(2.5)
                return {"ok": False, "status": -1, "len": 0}

            rec = await grab()
            cost = len(sent) - before
            if not rec.get("ok"):
                print(f"[{i}/{len(targets)}] {t['label']} ⚠️挑战页，重算 stoken")
                cost += await cold(t["id"])
                await asyncio.sleep(2.0)
                before2 = len(sent)
                rec = await grab()
                cost += len(sent) - before2
            if not rec.get("ok"):
                out.append({**t, "ok": False}); continue

            js = (rec.get("jobStatus") or "").strip()
            full = rec.get("fullText") or ""
            main = rec.get("mainText") or ""
            row = {
                **t, "ok": True,
                "h1": (rec.get("h1") or "").strip()[:30],
                "jobStatus": js,
                "jobStatus2": (rec.get("jobStatus2") or "").strip(),
                # ⚠️ 不能用 `bool(js)`：`.job-status` 容器**在招时也有文本**，
                # 值是「招聘中」。我最初按"有文本即关闭"判，于是 24 条全判关闭 ——
                # 而那批样本恰好一半真关闭，单看那一半是 10/10 满分。
                # 判据必须看**值**，不是看节点空不空。
                "by_container": _is_closed(js),
                "by_fulltext": _is_closed(full),
                "by_maintext": _is_closed(main),
                "active": (rec.get("activeTime") or "").strip() or None,
                "salary": (rec.get("salary") or "").strip(),
                "jdLen": rec.get("jdLen"),
                "requests": cost,
            }
            out.append(row)
            print(f"[{i}/{len(targets)}] {t['label']:<12} "
                  f"容器={row['by_container']!s:<5} 整篇={row['by_fulltext']!s:<5} "
                  f"主区={row['by_maintext']!s:<5} 薪资={row['salary']!r:<12} "
                  f"活跃={row['active']} JD={row['jdLen']}字")
            print(f"        h1={row['h1']!r} jobStatus={js!r}", flush=True)
            if i < len(targets):
                await asyncio.sleep(args.gap)

        await ctx.close()
        await browser.close()

    p = ROOT / args.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"visible_has_closed": "职位已关闭" in vis,
                             "results": out}, ensure_ascii=False, indent=2),
                 encoding="utf-8")

    ctrl = [r for r in out if r.get("ok") and r["label"] == "SEO在招"]
    kn = [r for r in out if r.get("ok") and r["label"] == "库内已关闭"]
    print("\n" + "=" * 66)
    print(f"对照组（平台保证在招）{len(ctrl)} 条:")
    print(f"  容器判关闭 {sum(r['by_container'] for r in ctrl)}"
          f" / 整篇判关闭 {sum(r['by_fulltext'] for r in ctrl)}"
          f" / 主区判关闭 {sum(r['by_maintext'] for r in ctrl)}")
    print(f"已知关闭 {len(kn)} 条:")
    print(f"  容器判关闭 {sum(r['by_container'] for r in kn)}"
          f" / 整篇判关闭 {sum(r['by_fulltext'] for r in kn)}"
          f" / 主区判关闭 {sum(r['by_maintext'] for r in kn)}")
    print()
    print(f"`.job-status` 实际取值分布:")
    for lab in ("SEO在招", "库内已关闭"):
        vals = {}
        for r in out:
            if r.get("ok") and r["label"] == lab:
                vals[r["jobStatus"]] = vals.get(r["jobStatus"], 0) + 1
        print(f"  {lab}: {vals}")
    print()
    ctrl_bad = sum(r["by_container"] for r in ctrl)
    kn_ok = sum(r["by_container"] for r in kn)
    if ctrl and kn and ctrl_bad == 0 and kn_ok == len(kn):
        print("✅ 判据有区分度：对照组全判在招、已知关闭全判关闭。")
        print("   `.job-status` 在招时是「招聘中」，关闭时是「职位已关闭」——")
        print("   所以要判**值**，不能判节点空不空。")
    elif ctrl and ctrl_bad:
        print(f"⛔ 对照组里有 {ctrl_bad} 条被判关闭，判据仍会误杀在招岗位。")
    else:
        print("⚠️ 样本不足或结论不一致，看逐条明细再判。")
    print(f"\n结果已存 {p}")


if __name__ == "__main__":
    asyncio.run(main())
