"""验证探针：登录态是否有效？

_hasPage 一直 False（window._PAGE 不注入），可能是登录态失效。本探针：
1. 读 context cookies，看 __zp_stoken__/bst/zp_at 是否还在
2. 用跨会话固定 token 发一次列表请求（列表接口零风控），看返回 code

code=0 有数据 → token 有效、登录态 OK → 是 _PAGE 注入时序问题，改 _read_token 兜底
code=37/32/36 → 登录态失效，需用户重新扫码

用法: python scripts/probe_login_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_radar.collectors.boss import BossCollector, ORIGIN, API_PATH  # noqa: E402
from job_radar.config import load_config  # noqa: E402

FIXED_TOKEN = "8GJVqmbrjhi7vZ28"  # 跨会话固定 token（记忆实测）

FETCH_TPL = """(payload) => {
    const {url, token} = payload;
    return fetch(url, {
        credentials: 'include',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'token': token,
    }).then(async r => ({status: r.status, text: await r.text()}))
    .catch(e => ({status: -1, text: '', error: String(e)}));
}"""


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    from patchright.async_api import async_playwright

    profile = str(Path(cfg.browser.user_data_dir).resolve())
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(
            f"{ORIGIN}/web/geek/jobs?query=AI%20Agent&city={cfg.cities[0].boss_code}",
            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)
        print(f"url: {page.url[:70]}", flush=True)

        # 1) cookie 检查
        cookies = await ctx.cookies()
        names = [c["name"] for c in cookies]
        key = ["__zp_stoken__", "bst", "zp_at", "wt2", "wb", "__zp_sseed__"]
        present = {k: (k in names) for k in key}
        print(f"cookies: {present}", flush=True)

        # 2) 真实请求验证（列表接口，零风控）
        city = cfg.cities[0]
        filters = cfg.filters[0] if cfg.filters else None
        collector = BossCollector(cfg)
        collector.context = ctx
        collector.page = page
        params = collector._params(city, cfg.keywords[0], filters, 1)
        url = f"{ORIGIN}{API_PATH}?{urlencode(params)}"
        for label, token in [("PAGE_token", ""), ("fixed_token", FIXED_TOKEN)]:
            result = await page.evaluate(FETCH_TPL, {"url": url, "token": token or None})
            text = result.get("text") or ""
            try:
                body = json.loads(text)
                code = body.get("code")
                n = len(((body.get("zpData") or {}).get("jobList") or []))
                msg = str(body.get("message") or "")[:50]
                print(f"[{label}] status={result.get('status')} code={code} jobs={n} msg={msg}", flush=True)
            except json.JSONDecodeError:
                print(f"[{label}] status={result.get('status')} 非JSON: {text[:80]}", flush=True)
            await page.wait_for_timeout(5000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
