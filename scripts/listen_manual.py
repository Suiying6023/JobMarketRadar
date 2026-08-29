"""被动监听人工操作时页面自己发出的 BOSS 接口请求。

本脚本**不发任何请求**，只挂 response 事件旁听。用途：
  1. 判断人工正常操作是否也被限流（是 -> 账号级窗口，与自动化姿势无关）
  2. 抓页面自己发请求时带的完整请求头，对比我们手动 fetch 缺了什么
  3. 顺手收集新鲜的 securityId / lid

用法: python scripts/listen_manual.py [监听秒数]
监听期间请在浏览器里正常操作，结束后结果落盘到 data/manual_listen/。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "manual_listen"
ORIGIN = "https://www.zhipin.com"

# 只关心业务接口，APM 埋点太吵，单独计数不打详情。
WATCH = (
    "/wapi/zpgeek/search/joblist.json",
    "/wapi/zpgeek/job/detail.json",
    "/wapi/zpgeek/pc/recommend/job/list.json",
    "/wapi/zpuser/wap/getUserInfo.json",
)
NOISE = "zpApm"

sys.path.insert(0, str(ROOT / "src"))
from job_radar.collectors.boss import RISK_TEXTS  # noqa: E402

# ⚠️ 本地副本已删（原先含「安全校验」，而正常 JD 里的「数据安全校验」会命中它，
# 把好数据标成 risk_hit —— boss.py 那份 2026-08-10 就移除了，副本没跟上）。


def stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def log(line: str) -> None:
    print(f"[{stamp()}] {line}", flush=True)


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    records: list[dict] = []
    noise_count = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]

        async def on_response(resp) -> None:
            nonlocal noise_count
            url = resp.url or ""
            if NOISE in url:
                noise_count += 1
                return
            hit = next((w for w in WATCH if w in url), None)
            if not hit:
                return

            entry: dict = {
                "at": datetime.now(timezone.utc).astimezone().isoformat(),
                "api": hit,
                "url": url,
                "status": resp.status,
                "request_headers": {},
                "code": None,
                "message": None,
                "jd_len": 0,
                "job_count": 0,
                "risk_hit": None,
                "salary_plaintext": None,
            }
            try:
                entry["request_headers"] = await resp.request.all_headers()
            except Exception as exc:
                entry["request_headers"] = {"_error": str(exc)}

            try:
                text = await resp.text()
            except Exception as exc:
                entry["_body_error"] = str(exc)
                records.append(entry)
                log(f"{hit} HTTP {resp.status} 正文读取失败: {exc}")
                return

            entry["risk_hit"] = next((t for t in RISK_TEXTS if t in text), None)
            try:
                body = json.loads(text or "{}")
            except json.JSONDecodeError:
                entry["_non_json_prefix"] = text[:200]
                records.append(entry)
                log(f"{hit} HTTP {resp.status} 非 JSON")
                return

            entry["code"] = body.get("code")
            entry["message"] = body.get("message")
            zp = body.get("zpData") or {}
            job_list = zp.get("jobList") or []
            entry["job_count"] = len(job_list)
            entry["jd_len"] = len(str((zp.get("jobInfo") or {}).get("postDescription") or ""))
            if job_list:
                entry["salary_plaintext"] = str(job_list[0].get("salaryDesc") or "")
                # 新鲜 token，后续 backfill 可用
                entry["fresh_tokens"] = [
                    {"securityId": j.get("securityId"), "lid": zp.get("lid"),
                     "encryptJobId": j.get("encryptJobId"), "jobName": j.get("jobName")}
                    for j in job_list[:5]
                ]
            if zp.get("securityId"):
                entry["detail_fresh_securityId"] = str(zp["securityId"])

            records.append(entry)
            flag = f" 风控命中「{entry['risk_hit']}」" if entry["risk_hit"] else ""
            extra = ""
            if entry["job_count"]:
                extra = f" 岗位={entry['job_count']} 薪资={entry['salary_plaintext']!r}"
            elif entry["jd_len"]:
                extra = f" JD={entry['jd_len']}"
            log(f"{hit} HTTP {resp.status} code={entry['code']}{extra}{flag}")

        ctx.on("response", lambda r: asyncio.create_task(on_response(r)))

        pages = [p.url for p in ctx.pages if ORIGIN in (p.url or "")]
        log(f"已挂监听，zhipin 页签: {pages}")
        log(f"本脚本不发任何请求。请开始手动操作，监听 {seconds:.0f} 秒。")
        log("建议：搜『AI Agent』+杭州 -> 翻 1-2 页 -> 点开 1-2 个岗位")

        await asyncio.sleep(seconds)

    out = OUT_DIR / f"manual-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"=== 监听结束：业务接口 {len(records)} 次，APM 埋点 {noise_count} 次 ===")
    risky = [r for r in records if r["risk_hit"]]
    if risky:
        log(f"其中 {len(risky)} 次命中风控关键字 -> 人工操作同样被限流")
    elif records:
        log("没有命中风控关键字 -> 人工操作此刻是通的")
    else:
        log("没抓到任何业务接口请求（可能页面被卡在验证层，压根没发出请求）")
    log(f"完整结果: {out}")


if __name__ == "__main__":
    asyncio.run(main())
