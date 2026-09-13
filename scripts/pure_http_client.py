"""纯 HTTP 客户端：脱离浏览器直接调 BOSS 详情接口。

背景：2026-08-01 发现 BOSS 前端检测 CDP 连接并触发自动刷新（消耗账号信用）。
方案 A：一次性从浏览器拿到完整 cookie + token，之后用纯 HTTP 请求，
完全不连 CDP，从而不触发检测。

原料来源：
- cookie jar：从 data/manual_listen/ 里记录的 detail.json 请求头
- token：window._PAGE.token 的固定值（跨会话不变）
- securityId / lid：库里已有

用法:
    python scripts/pure_http_client.py --cookie-file data/manual_listen/manual-20260730-165048.json
    python scripts/pure_http_client.py  # 自动从落盘提取

注意：封禁期不要跑。等 04:05 解锁后，先用 1 个请求验证。
"""
from __future__ import annotations

import argparse
import json
import glob
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "hangzhou_agent.sqlite3"
sys.path.insert(0, str(ROOT / "src"))
ORIGIN = "https://www.zhipin.com"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"

from job_radar.collectors.boss import RISK_TEXTS  # noqa: E402

# ⚠️ 本地副本已删（原先含「安全校验」，正常 JD 里的「数据安全校验」会命中它）。


def load_cookie_jar(manual_file: str | None) -> dict[str, str]:
    """从落盘的 detail.json 请求头里提取完整 cookie。"""
    if manual_file:
        files = [manual_file]
    else:
        files = sorted(glob.glob(str(ROOT / "data/manual_listen/*.json")))
    for f in files:
        data = json.load(open(f, encoding="utf-8"))
        for entry in data:
            hdrs = entry.get("request_headers") or {}
            if "detail.json" in entry.get("api", ""):
                ck = hdrs.get("cookie", "")
                parts = dict(p.strip().split("=", 1) for p in ck.split(";") if "=" in p)
                print(f"[cookie] 从 {Path(f).name} 提取 {len(parts)} 个 cookie")
                return parts
    raise SystemExit("没找到含 detail.json cookie 的落盘文件")


def pick_target(limit: int = 1) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT title, raw_json FROM jobs "
        "WHERE (job_description IS NULL OR job_description = '') "
        "ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        if raw.get("securityId"):
            out.append((r["title"], str(raw["securityId"]), str(raw.get("lid") or "")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie-file", default=None)
    parser.add_argument("--token", default="8GJVqmbrjhi7vZ28")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.0,
                        help="每条请求之间的间隔秒数，防连发触发节流")
    args = parser.parse_args()

    jar = load_cookie_jar(args.cookie_file)
    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())

    targets = pick_target(args.count)
    if not targets:
        print("库里没有可用的 securityId 目标")
        return
    print(f"目标: {targets[0][0]}")

    # 构造与页面请求一致的 headers
    # zp_token = cookie bst（真实请求显式发，不是靠 cookie 自动带）；
    # traceid 每次请求重新生成，放到循环内设置。
    zp_token = jar.get("bst", "")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_str,
        "token": args.token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.zhipin.com/web/geek/jobs?query=AI%20Agent&city=101210100",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if zp_token:
        headers["zp_token"] = zp_token

    # 用标准库 urllib 发请求（不引第三方依赖；requests 可能没装）
    import urllib.request

    from job_radar.traceid import generate as gen_traceid

    ok = fail = 0
    for i, (title, sid, lid) in enumerate(targets, start=1):
        url = f"{ORIGIN}{DETAIL_PATH}?{urlencode({'securityId': sid, 'lid': lid})}"
        req_headers = dict(headers)
        req_headers["traceid"] = gen_traceid()
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except Exception as exc:
            print(f"[{i}] 请求异常: {exc}")
            fail += 1
            continue

        hit = next((t for t in RISK_TEXTS if t in raw), None)
        if hit:
            print(f"[{i}] FAIL 风控命中「{hit}」: {raw[:200]}")
            fail += 1
            break  # 命中节流立即停止，不连发后续请求（风控纪律）

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[{i}] 非 JSON: {raw[:200]}")
            fail += 1
            continue

        code = body.get("code")
        jd = (((body.get("zpData") or {}).get("jobInfo") or {}).get("postDescription") or "")
        if code == 0 and jd:
            print(f"[{i}] OK HTTP {status} code=0 JD={len(jd)} {title[:30]}")
            ok += 1
        else:
            print(f"[{i}] 异常 HTTP {status} code={code} 无JD: {raw[:150]}")
            fail += 1

        if i < len(targets) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\n成功 {ok} 失败 {fail}")


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    main()
