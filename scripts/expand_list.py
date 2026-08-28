"""列表扩充：用新筛选跑多关键词多页列表接口，扩充岗位池并刷新存活。

只抓列表接口（零风控），不抓详情。把符合目标画像的岗位 upsert 进库，
同时刷新 last_seen_at 确认存活。列表接口本身字段含薪资/公司/经验/学历，
足够做筛选和初筛。

用法: python scripts/expand_list.py [--pages N] [--delay S]
  --pages  每个关键词抓多少页（默认 5）
  --delay  每页间隔秒数（默认 8，列表接口零风控可放松）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.collectors.boss import BossCollector, ORIGIN, API_PATH  # noqa: E402
from job_radar.config import load_config  # noqa: E402
from job_radar.storage import JobStore  # noqa: E402

# ⚠️ 2026-08-10 两处修复（详见 boss.py:_evaluate_fetch 注释）：
# ① headers 必须嵌在 init 的 `headers` 键里，否则 fetch 静默丢弃非白名单键
# ② 前端真实请求**不发 token 头**，发的是 zp_token（= cookie bst）+ x-requested-with
FETCH_TPL = """(payload) => {
    const {url} = payload;
    const readCookie = (name) => {
        const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
    };
    const headers = {
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
    };
    const bst = readCookie('bst');
    if (bst) headers['zp_token'] = bst;
    return fetch(url, {credentials: 'include', headers})
    .then(async r => ({status: r.status, text: await r.text()}))
    .catch(e => ({status: -1, text: '', error: String(e)}));
}"""

# 列表接口零风控的结论已过时（2026-08-02 实测：信任度降低后约 5 个请求触发 code=37）。
# code=37 是 stoken 过期，可走 security-check 恢复后重试。
# ⚠️ 2026-08-10：FIXED_TOKEN 已废弃不用。被动监听证明前端**从不发 token 头**，
# 这个「固定 token 设备绑定」的说法建立在旧的 header bug 之上（那时头根本没发出去，
# 所谓「探针验证有效」的 A/B 两组实际是同一组）。正确凭证是 zp_token（= cookie bst），
# 页面内现读，见 FETCH_TPL。

# 对齐 boss.py：不能含「安全校验」——列表接口虽无 JD 正文，但保持口径一致防误报。
RISK_TEXTS = ("访问频繁", "环境存在异常", "账户存在异常",
              "操作过于频繁", "操作太频繁", "滑块")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.hangzhou.yaml", help="配置文件（可指向试验配置）")
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--delay", type=float, default=8.0)
    parser.add_argument("--start", type=int, default=0,
                        help="从第几个关键词开始（0-based，跳过已完成的词）")
    parser.add_argument("--end", type=int, default=None,
                        help="到第几个关键词结束（0-based，不含），默认全跑")
    # ⚠️ 默认从 2 提到 5（2026-08-11 实测）。BOSS 的搜索结果是
    # **浅层各词共享、深层各词独有**：前 1-3 页是所有相近关键词都会返回的热门岗位
    # （早被采过 → 零新增），第 4 页之后才进入该词独有的长尾。实测三个词同一模式：
    #   后端开发工程师  页1-3: 3,2,0  → 页4-10: 9,20,28,29,30,30,30
    #   向量数据库      页1-3: 2,1,5  → 页4-8:  14,23,24,25,21
    #   Python开发工程师 页1-3: 0,0,1  → 页5-7:  4,7,9
    # `后端开发工程师` 页3 恰好是 0 —— dry-stop=2 或 3 会**在最富的矿脉前掉头**，
    # 而且因为 dry-stop 提前收尾又不推进进度，这个词会被永久判成"已饱和"。
    parser.add_argument("--dry-stop", type=int, default=5,
                        help="连续几页零新增才换关键词（默认 5：浅层零新增是常态，"
                             "深层才出货，设太小会在富矿前掉头）")
    parser.add_argument("--restart", action="store_true",
                        help="忽略断点进度，所有关键词从第 1 页重新翻"
                             "（平台岗位有轮换，隔一段时间重扫浅层有意义）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # ⚠️ 遍历**全部**城市。原先只取 cities[0]，配置里写三个城市它也只抓第一个——
    # 静默跑三倍时间抓同一批数据，而日志看不出任何异常（2026-08-11 扩沪深时发现）。
    cities = cfg.cities
    filters = cfg.filters[0] if cfg.filters else None
    keywords = cfg.keywords[args.start:args.end]
    if not keywords:
        print(f"关键词切片为空: start={args.start} end={args.end}")
        return
    print(f"本轮城市({len(cities)}个): {[c.name for c in cities]}")
    print(f"本轮关键词({len(keywords)}个): {keywords}")

    from patchright.async_api import async_playwright

    collector = BossCollector(cfg)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(Path(cfg.browser.user_data_dir).resolve()),
            channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000"],
            ignore_default_args=["--enable-automation"])
        collector.context = ctx
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        collector.page = page
        await page.goto(
            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={cities[0].boss_code}",
            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)
        print(f"页签: {page.url[:80]}")

        with JobStore(cfg.database_path) as store:
            from job_radar.throttle import HumanThrottle
            throttle = HumanThrottle(cfg)
            # zp_token（= cookie bst）在页面内现读，不需要 Python 侧传，见 FETCH_TPL。
            seen_new = seen_total = 0
            recover_count = 0  # code=37 恢复次数，过高即熔断（防封号）
            # 请求账本：这个脚本自己 evaluate（不走 collector._evaluate_fetch），
            # 所以必须自己记账，否则它发的请求在日线上完全隐形——三个脚本里
            # 只有它绕过了统一入口，恰恰又是最容易连跑多轮的那个。
            collector.ledger = store
            collector.ledger_script = "expand_list"
            print(f"今日账本：{store.request_counts()}，单日上限 {cfg.max_requests_per_day}")
            # 城市 × 关键词。城市在外层：同城连续翻更像真人（一个人不会
            # 杭州翻一页就跳深圳），而且 lid 会话串联也是按城市连贯的。
            for ci, city in enumerate(cities):
                if len(cities) > 1:
                    print(f"\n=== 城市 {city.name}（{city.boss_code}）===")
                    # 换城市要真的把页面导过去。同源 fetch 是在当前页面里发的，
                    # 虽然 city 参数由 URL 决定、不导航也能拿到数据，但页面上下文
                    # 与请求参数不一致本身就是可被识别的异常（真人换城市必然刷页）。
                    if ci > 0:
                        await page.goto(
                            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={city.boss_code}",
                            wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(3000)
                        collector.last_lid = ""   # lid 属于上一个城市的搜索会话
                        store.log_request("list", script="expand_list",
                                          note=f"nav_{city.name}")
                for kw in keywords:
                    dry_pages = 0  # 连续零新增页数，每换关键词重置
                    # 断点续翻：从上次翻到的下一页开始。
                    # 不这么做的话 `--pages N` 是**永久天花板**而不是单轮上限——
                    # 第二轮跑同一个词，前几页必然全是已见岗位 → 立刻触发
                    # dry-stop 跳过 → 页 N+1 以后永远到不了。
                    # 实测 `向量数据库` 深翻到页 7 仍有 25 条/页新增，被截断的量很大。
                    done_page, exhausted = store.get_keyword_progress(city.name, kw)
                    if exhausted and not args.restart:
                        print(f"[{city.name}|{kw}] 已确认翻到底（第 {done_page} 页），跳过")
                        continue
                    start_page = 1 if args.restart else done_page + 1
                    end_page = start_page + args.pages - 1
                    if start_page > 1:
                        print(f"[{city.name}|{kw}] 续翻 第 {start_page}-{end_page} 页")
                    last_page_done = done_page
                    for p in range(start_page, end_page + 1):
                        used = store.request_counts().get("total", 0)
                        if used >= cfg.max_requests_per_day:
                            print(f"⛔ 今日累计请求 {used}/{cfg.max_requests_per_day}，"
                                  f"达上限主动停止（封禁按累计量算）")
                            break
                        # 请求前节流：高斯延迟 + 突发惩罚（账号信任度已降低，不能无脑快抓）
                        await throttle.delay_before_request("list")
                        params = collector._params(city, kw, filters, p)
                        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
                        text = ""
                        store.log_request("list", script="expand_list")
                        # code=37（环境存在异常）可走 security-check 恢复后重试，最多 2 次
                        for attempt in range(1, 3):
                            try:
                                result = await page.evaluate(FETCH_TPL, {"url": url})
                            except Exception as exc:  # noqa: BLE001
                                # evaluate 偶发"Execution context destroyed"（BOSS 加载跳转），重试即可
                                print(f"[{kw}|页{p}] evaluate 异常(第{attempt}次): {str(exc)[:60]}，重试")
                                await page.wait_for_timeout(2000)
                                continue
                            status = int(result.get("status", 0))
                            text = result.get("text") or ""
                            hit = next((t for t in RISK_TEXTS if t in text), None)
                            if hit == "环境存在异常":
                                # 解析 code=37 的 seed/name/ts，复用 boss.py 的恢复路径
                                try:
                                    body = json.loads(text or "{}")
                                    zp = body.get("zpData") or {}
                                    seed = str(zp.get("seed") or "")
                                    name = str(zp.get("name") or "")
                                    ts = str(zp.get("ts") or "")
                                except json.JSONDecodeError:
                                    seed = name = ts = ""
                                if seed and name and ts:
                                    recover_count += 1
                                    # ⚠️ 阈值原为硬编码 12，是"code=37 频率能预警封禁"
                                    # 这个**已被推翻的旧认知**的遗留。2026-08-11 实测：
                                    # code=37 是无记忆配额窗口（每约 6 个请求一次，
                                    # 密度不随累计量上升），封禁走的是另一套机制。
                                    # 12 次 ≈ 只够翻 70 多页，实测在 `Python开发工程师`
                                    # 页10 触发并**掐断整轮**——12 个新关键词和上海/深圳
                                    # 全都没跑到，而那个词页8/9 还在出 13 条/页。
                                    # 改用配置里的 code37_limit（稳定档 200），
                                    # 真正的安全线由 max_requests_per_day 兜。
                                    if recover_count > cfg.code37_limit:
                                        print(f"[{kw}|页{p}] code=37 恢复 {recover_count} 次"
                                              f"超阈值 {cfg.code37_limit}，停止整轮")
                                        await ctx.close()
                                        sys.exit(2)  # 2=熔断，wrapper 识别后整轮停止
                                    print(f"[{kw}|页{p}] code=37 第 {attempt} 次重算 stoken 后重试（累计 {recover_count}）")
                                    try:
                                        # _recover_code37 内部会自己记 security_check 账
                                        await collector._recover_code37(seed, name, ts)
                                    except Exception as exc:  # noqa: BLE001
                                        print(f"[{kw}|页{p}] code=37 恢复异常: {str(exc)[:60]}，跳过本页")
                                        break
                                    # 重算后要再发一次列表请求，也得记账
                                    store.log_request("list", script="expand_list",
                                                      note="code37_retry")
                                    continue
                                print(f"[{kw}|页{p}] code=37 缺 seed/name/ts，无法恢复")
                                break
                            if hit:
                                print(f"[{kw}|页{p}] 风控「{hit}」: {text[:100]}")
                                break
                            break  # 无风控，跳出重试循环
                        if hit:
                            break  # 重试耗尽或非 code=37，跳过本关键词后续页
                        try:
                            body = json.loads(text or "{}")
                        except json.JSONDecodeError:
                            print(f"[{kw}|页{p}] 非 JSON: {text[:80]}")
                            break
                        code = body.get("code")
                        jobs = ((body.get("zpData") or {}).get("jobList") or [])
                        if not jobs:
                            # 空 jobList = 真的翻到底了（不是"这几页没新货"）。
                            # 标 exhausted，下次直接跳过这个词，不再浪费请求。
                            print(f"[{kw}|页{p}] code={code} 无岗位，确认翻到底")
                            store.set_keyword_progress(city.name, kw, p - 1,
                                                       exhausted=True)
                            break
                        page_new = page_active = 0
                        for rank, raw in enumerate(jobs, start=1):
                            job = collector._parse_api_job(raw, kw, rank)
                            # 全保留分层：无效岗位跳过；画像外岗位标 sample 入库作去重样本。
                            if not job.title or not job.job_url:
                                continue
                            job.retention_status = "active" if collector._should_keep(job) else "sample"
                            status_up = store.upsert(job)
                            seen_total += 1
                            if status_up == "inserted":
                                seen_new += 1
                                page_new += 1
                                if job.retention_status == "active":
                                    page_active += 1
                        # 本页新增里有多少是画像内的：新增全是 sample 说明这个关键词跑题，
                        # 命中率连续为 0 的关键词该从 config 里砍掉（省整轮列表请求）。
                        print(f"[{kw}|页{p}] {len(jobs)} 条 → 本页新增 {page_new}（画像内 {page_active}），"
                              f"累计 {seen_total}，新增 {seen_new}")
                        # 提前收尾：连续 N 页零新增就换词。
                        # 深翻（--pages 10+）时这个判断是必需的——列表和详情共享配额窗口
                        # （2026-08-10 实测），每页都在烧同一份预算，翻到全是老岗位纯属浪费。
                        # 要求"连续"而不是单页，因为平台偶尔会插入一页全旧结果。
                        # 每页都记进度，进程被杀也不丢（本轮已翻的页不用重翻）
                        last_page_done = p
                        store.set_keyword_progress(city.name, kw, p,
                                                   last_new=page_new)
                        if page_new == 0:
                            dry_pages += 1
                            if dry_pages >= args.dry_stop and p >= 2:
                                # 注意：dry-stop 只说明"这几页没新货"，**不等于翻到底**，
                                # 所以不标 exhausted —— 下次仍从 p+1 续翻。
                                # 平台会往深层塞新岗位，实测 `向量数据库` 页 4-9 才是
                                # 富矿区（每页 14-25 条新增）。
                                print(f"[{kw}] 连续 {dry_pages} 页零新增，"
                                      f"跳到下个关键词（进度存于第 {p} 页，下次续翻）")
                                break
                        else:
                            dry_pages = 0
                        # 关键词切换时额外随机停顿，模拟人翻完一类再换
                        if p < end_page:
                            await page.wait_for_timeout(throttle._gaussian(args.delay, args.delay + 4) * 1000)

    print(f"\n列表扩充完成: 共处理 {seen_total} 条，新增 {seen_new} 条")
    # code=37 恢复导航后 close 偶发 TargetClosedError，数据已入库，忽略即可
    try:
        await ctx.close()
    except Exception as exc:  # noqa: BLE001
        print(f"(ctx.close 竞态，数据已入库): {str(exc)[:60]}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
