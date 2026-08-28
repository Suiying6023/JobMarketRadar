"""重新抓取 f1a4ba9af69a3edb0nF93tq9GVZV 的最新 JD，检查是否更新了毕业时间/截止信息。

郭辉 profile 打开岗位页 + 页面内调 detail API，对比 postDescription。仅 1 次导航 + 1 次 API。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from job_radar.collectors.browser import BrowserSession  # noqa: E402
from job_radar.config import load_config  # noqa: E402

JOB_URL = "https://www.zhipin.com/job_detail/f1a4ba9af69a3edb0nF93tq9GVZV.html"
SECURITY_ID = "ysAS5vlzJ9BRD-C1_uOgxjbDO0bJKKMdj_R50W1u7d_XNsvwh6GyJhZPRoUNTuucPjFVz5SDTXwtN2cgZjlMaR5jfrd2EgIlZ1cq3ifJ-XESG5YW-SUvBz4BBRIdPjFd6vhLZiW14W-widj-vLTWdqrjkq4NjxBudtWos51rXGPT30m40Gvjw-51HtBjj6eY9eoc2N8xF9diOqmXU2EQi7lXbKSWg0idtXSVR7xGLnZkRzpkc7uqqRHRF9HzkgrdWukLokrz-vRaOiw4J1PbfiM8Kv9sTso3kJ0mVsTp_hEw_TEfi9LzrtaP4j-xgm8Zdmy9DOJExblnnZM2xHzOkxezzuhTkJKynoy2GmbLKwtIE0896UVP68b8_QRws75HaZuX_YrrS2EeXNM225a3laHMAx_DYSc~"
LID = "5M73wgMPzI3.search.18"
KW = ["毕业时间", "招聘截止", "2027", "27届", "校招", "截止", "2027届"]


async def main() -> None:
    cfg = load_config("config.hangzhou.yaml")
    sess = BrowserSession(cfg.browser)
    ctx = await sess.start()
    page = await ctx.new_page()
    try:
        await page.goto(JOB_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(6000)
        print("URL:", page.url[:110], flush=True)
        # 1) 页面渲染文本
        ptext = await page.evaluate("document.body ? document.body.innerText : ''")
        for kw in KW:
            i = ptext.find(kw)
            if i >= 0:
                print(f"[页面] {kw}: ...{ptext[max(0,i-30):i+50].replace(chr(10),' ')}", flush=True)
        # 2) detail API 最新 postDescription
        res = await page.evaluate(
            """async (a) => {
                const url = `/wapi/zpgeek/job/detail.json?securityId=${a.sid}&lid=${a.lid}`;
                const r = await fetch(url, {headers: {"x-requested-with": "XMLHttpRequest"}});
                return {code: r.status, text: await r.text()};
            }""", {"sid": SECURITY_ID, "lid": LID})
        try:
            body = json.loads(res["text"])
            zp = body.get("zpData") or {}
            ji = zp.get("jobInfo") or {}
            pd = ji.get("postDescription") or ""
            print(f"\n[API] code={body.get('code')} | postDescription 长度: {len(pd)}", flush=True)
            for kw in KW:
                if kw in pd:
                    i = pd.find(kw)
                    print(f"[API] {kw}: ...{pd[max(0,i-30):i+50].replace(chr(10),' ')}", flush=True)
            print("[API] 新 postDescription 是否含毕业时间:", "毕业时间" in pd, flush=True)
            # jobInfo 里找毕业时间相关新字段
            print("[API] jobInfo keys:", list(ji.keys()), flush=True)
        except Exception as e:
            print("[API] 解析失败:", str(e)[:150], res["text"][:200], flush=True)
    except Exception as e:  # noqa: BLE001
        print("ERR:", type(e).__name__, str(e)[:200], flush=True)
    await sess.close()


if __name__ == "__main__":
    asyncio.run(main())
