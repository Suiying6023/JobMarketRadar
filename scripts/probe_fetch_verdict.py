"""验证「同源 fetch HTML + 精确选择器」的判定是否可信 —— 用**双向已知答案**核对。

`probe_html_fetch.py` 已证明 fetch 一条 = 1 次请求，但它用「整篇文本找子串」判定，
5 条里 4 条报「职位已关闭」而库里都是在招。`probe_html_structure.py` 找到了真正的
容器：`.job-status`（关闭状态）与 `.boss-active-time`（招聘者活跃度），都是全页唯一。

**但「找到了选择器」不等于「判定正确」**：那 4 条到底是真关闭（库里的快照过期了，
它们本来就是 7-30 天前采的），还是解析仍有问题？只看单向样本无法区分 ——
一个恒返回"关闭"的坏判据，在一批真关闭的样本上会 100% "正确"。

所以取**两组都知道答案的样本**：
  · 库里 `job_status_desc='职位已关闭'` 的 → 期望判「关闭」
  · 昨天（08-29）刚抓的 active → 绝大多数应判「在招」
两组都对，判据才成立。只用一组等于没验证。

⚠️ 免登录。冷启动整页加载 1 次拿会话 stoken，其余全走 fetch（每条 1 次请求）。
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
from check_job_status import ACTIVE_DAYS  # noqa: E402

BLOCK_TYPES = {"image", "media", "font"}
BLOCK_HOSTS = ("logapi.zhipin.com", "geetest.com", "apm-fe.zhipin.com",
               "hm.baidu.com", "shink.zhipin.com", "t.zhipin.com")

# 按**容器**取值，不在整篇文本里找子串。三个字段各有全页唯一的节点：
#   .job-status         关闭横幅（在招岗位这个节点为空或不存在）
#   .boss-active-time   当前岗位招聘者的活跃度（推荐位里别人的活跃度不在这个类下）
#   .job-sec-text       JD 正文（顺带取，用于核对拿到的是不是这条岗位）
# 另取 h1 标题做**身份校验**：确认 fetch 回来的页面确实是请求的那条岗位，
# 而不是被重定向到了别处（换页/挑战/推荐页都会让判定张冠李戴）。
EXTRACT = """async ({url}) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    if (html.length < 60000) {
        return {ok: false, reason: 'challenged', status: r.status, len: html.length};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const txt = (sel) => {
        const el = doc.querySelector(sel);
        return el ? (el.textContent || '').trim() : null;
    };
    return {
        ok: true, status: r.status, len: html.length,
        h1: txt('.info-primary .name h1') || txt('h1'),
        jobStatus: txt('.job-banner .job-status'),
        activeTime: txt('.boss-active-time'),
        bossName: txt('.job-boss-info h2.name'),
        jdLen: (txt('.job-sec-text') || '').length,
        salary: txt('.info-primary .salary'),
    };
}"""


def classify(rec: dict) -> tuple[bool | None, str | None]:
    """把抽到的字段翻成 (是否关闭, 活跃度文案)。

    关闭判据：`.job-status` 节点**有文本**即关闭。在招岗位该节点为空串或不存在，
    所以不能用「节点存在」判，要用「文本非空」判。
    """
    if not rec.get("ok"):
        return None, None
    st = (rec.get("jobStatus") or "").strip()
    closed = bool(st)
    act = (rec.get("activeTime") or "").strip() or None
    if act and act not in ACTIVE_DAYS:
        act = next((p for p in sorted(ACTIVE_DAYS, key=len, reverse=True)
                    if p in act), act)
    return closed, act


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--closed", type=int, default=6, help="已知关闭的取几条")
    ap.add_argument("--fresh", type=int, default=8, help="昨天刚抓的 active 取几条")
    ap.add_argument("--gap", type=float, default=7.0)
    ap.add_argument("--out", default="data/calibration/fetch-verdict.json")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    closed_rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status, "
        "       job_status_desc, 1 AS expect_closed FROM jobs "
        "WHERE job_status_desc='职位已关闭' ORDER BY detail_fetched_at DESC LIMIT ?",
        (args.closed,)).fetchall()
    fresh_rows = con.execute(
        "SELECT source_job_id, title, recruiter_active_status, "
        "       job_status_desc, 0 AS expect_closed FROM jobs "
        "WHERE retention_status='active' AND job_invalid=0 "
        "  AND job_status_desc!='职位已关闭' AND job_description!='' "
        "ORDER BY detail_fetched_at DESC LIMIT ?", (args.fresh,)).fetchall()
    con.close()
    rows = list(closed_rows) + list(fresh_rows)
    print(f"样本: 已知关闭 {len(closed_rows)} 条 + 昨天刚抓 {len(fresh_rows)} 条\n")

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

        # 冷启动：整页加载一条（用第一条），让 JS 挑战自解拿到会话 stoken。
        # 判据只认「拿到了 __zp_stoken__ cookie」，不看标题：
        # 关闭岗位的标题同样含「招聘」（实测「…工程师招聘工资_…-BOSS直聘」），
        # 但更重要的是 stoken 才是后面 fetch 真正依赖的东西 —— 直接检查它，
        # 比从标题反推「大概拿到了」可靠。
        url0 = f"https://www.zhipin.com/job_detail/{rows[0]['source_job_id']}.html"
        try:
            await page.goto(url0, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        loop = asyncio.get_event_loop()
        start = loop.time()
        landed = ""
        while loop.time() - start < 60:
            names = {c["name"] for c in await ctx.cookies()}
            if "__zp_stoken__" in names:
                landed = "ok"; break
            try:
                t = await page.title()
            except Exception:
                await asyncio.sleep(1.0); continue
            if "安全验证" in t or "/verify.html" in (page.url or ""):
                landed = "verify"; break
            await asyncio.sleep(1.5)
        cold = len(sent)
        print(f"[冷启动] 落点={landed}（判据=拿到 __zp_stoken__）请求={cold} 次\n")
        if landed != "ok":
            print("冷启动没拿到 stoken，退出。")
            await ctx.close(); await browser.close(); return

        hit = miss = challenged = 0
        for i, r in enumerate(rows, 1):
            url = f"https://www.zhipin.com/job_detail/{r['source_job_id']}.html"
            before = len(sent)
            rec = await page.evaluate(EXTRACT, {"url": url})
            cost = len(sent) - before
            closed, act = classify(rec)
            expect = bool(r["expect_closed"])
            # 身份校验：h1 必须和库里标题对得上，否则这条判定不能采信
            h1 = (rec.get("h1") or "").strip()
            same_job = bool(h1) and (h1[:8] in r["title"] or r["title"][:8] in h1)
            if not rec.get("ok"):
                challenged += 1
                mark = "⚠️挑战页"
            elif closed == expect:
                hit += 1
                mark = "✅"
            else:
                miss += 1
                mark = "❌"
            out.append({"job_id": r["source_job_id"], "title": r["title"][:30],
                        "expect_closed": expect, "got_closed": closed,
                        "active": act, "db_active": r["recruiter_active_status"],
                        "h1": h1[:30], "same_job": same_job,
                        "requests": cost, "raw": rec})
            print(f"[{i}/{len(rows)}] {mark} 期望{'关闭' if expect else '在招'} "
                  f"→ 判{'关闭' if closed else ('在招' if closed is False else '?')}"
                  f"  活跃度={act}（库存 {r['recruiter_active_status']}）"
                  f"  身份符={same_job} 请求={cost}")
            print(f"        {r['title'][:26]} | h1={h1[:26]!r} "
                  f"jobStatus={rec.get('jobStatus')!r}", flush=True)
            if i < len(rows):
                await asyncio.sleep(args.gap)

        await ctx.close()
        await browser.close()

    p = ROOT / args.out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cold_requests": cold, "results": out},
                            ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"判定正确 {hit}/{len(rows)}，错 {miss}，挑战页 {challenged}")
    got_act = sum(1 for r in out if r["active"])
    print(f"拿到活跃度 {got_act}/{len(rows)}")
    bad_id = [r for r in out if r["raw"].get("ok") and not r["same_job"]]
    print(f"身份不符 {len(bad_id)} 条" + (f" ← 必须查明: {[r['job_id'] for r in bad_id]}"
                                        if bad_id else ""))
    print(f"总成本 {cold} + {len(rows)}×1 = {cold + len(rows)} 次请求"
          f"（整页加载同样条数需 {len(rows)*102} 次）")
    print(f"结果已存 {p}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    asyncio.run(main())
