"""清场：杀掉本项目遗留的自动化浏览器与跑批进程。

**为什么必须有这个脚本**：用户 2026-08-31 反馈
「最后一次我强行停止的时候似乎还是有 boss 无限刷新验证」。
`Stop-Process -Force` / SIGKILL 不给 Python 运行 finally 的机会，
于是 Playwright 起的 Chrome 变成**孤儿进程**留在后台 —— 页面还在自己
重试验证，也就是在无人知晓的情况下持续打平台。那比浪费配额糟得多：
**孤儿浏览器的请求不进任何账本**，等于绕过我们全部的限速、日线与降速档位，
而且没人知道它在跑 —— 排查时看到的用量与真实用量不符。

**判据必须精确到"是我们起的"**，绝不能按进程名一把梭：
用户自己的 Chrome/Edge 有几十个进程，误杀会关掉他正在用的标签页。
所以只认两个特征之一：
  ① 命令行含 `--disable-blink-features=AutomationControlled`
     （本项目所有 launch 都带它，且日常浏览器不会有）
  ② 命令行含本项目的 profile 路径（`.browser-profile`）
另外 `--remote-debugging-port` 也算，那是 Playwright 的固定特征。

用法：
    uv run python scripts/cleanup_browsers.py          # 只报告，不杀
    uv run python scripts/cleanup_browsers.py --kill   # 真的杀
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只有带这些特征的浏览器才是我们起的。顺序无关，命中任一即算。
BROWSER_MARKERS = (
    "--disable-blink-features=AutomationControlled",
    "--remote-debugging-port",
    ".browser-profile",
    "ms-playwright",
)

# 本项目的跑批脚本。清场时一并停掉，否则它们会立刻再起一个浏览器。
SCRIPT_MARKERS = (
    "refresh_status_loop",
    "backfill_details",
    "overnight_supervisor",
    "manual_verify_proxy",
    "check_account_status",
    "expand_list",
    "probe_",
)


def _ps(cmd: str) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                         capture_output=True, text=True, timeout=120)
    return out.stdout or ""


def find_procs() -> tuple[list[dict], list[dict]]:
    """返回 (浏览器进程, 跑批进程)。用 CIM 拿 CommandLine —— Get-Process 没有它。"""
    raw = _ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in @('chrome.exe','msedge.exe','chromium.exe',"
        "'python.exe','node.exe') } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 3")
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return [], []
    if isinstance(data, dict):
        data = [data]

    browsers, scripts = [], []
    for p in data:
        cmd = p.get("CommandLine") or ""
        name = (p.get("Name") or "").lower()
        if name in ("chrome.exe", "msedge.exe", "chromium.exe"):
            if any(m in cmd for m in BROWSER_MARKERS):
                browsers.append(p)
        elif name == "python.exe":
            if any(m in cmd for m in SCRIPT_MARKERS):
                scripts.append(p)
    return browsers, scripts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", action="store_true",
                    help="真的杀掉（默认只报告）")
    ap.add_argument("--keep-serve", action="store_true", default=True,
                    help="保留 serve_pool.py（投递池 HTTP 服务，与平台无关）")
    args = ap.parse_args()

    browsers, scripts = find_procs()
    # serve_pool 不碰平台，默认留着 —— 它是你看投递池用的。
    scripts = [s for s in scripts
               if not (args.keep_serve and "serve_pool" in (s.get("CommandLine") or ""))]

    print(f"本项目自动化浏览器: {len(browsers)} 个")
    for b in browsers:
        cmd = (b.get("CommandLine") or "")[:100]
        print(f"  pid={b['ProcessId']:<8} {b['Name']}  {cmd}")
    print(f"本项目跑批进程: {len(scripts)} 个")
    for s in scripts:
        cmd = (s.get("CommandLine") or "")
        tail = cmd.split("scripts")[-1][:70] if "scripts" in cmd else cmd[:70]
        print(f"  pid={s['ProcessId']:<8} scripts{tail}")

    if not browsers and not scripts:
        print("\n✅ 现场是干净的，没有遗留进程。")
        return

    if not args.kill:
        print("\n（只报告，未清理。加 --kill 真的停掉）")
        return

    # 先停跑批脚本，再杀浏览器：反过来的话脚本会立刻再起一个。
    for group, label in ((scripts, "跑批"), (browsers, "浏览器")):
        for p in group:
            pid = p["ProcessId"]
            _ps(f"Stop-Process -Id {pid} -Force -Confirm:$false "
                f"-ErrorAction SilentlyContinue")
        if group:
            print(f"已停 {label} {len(group)} 个")

    left_b, left_s = find_procs()
    left_s = [s for s in left_s if "serve_pool" not in (s.get("CommandLine") or "")]
    print(f"\n复查：浏览器 {len(left_b)} 个，跑批 {len(left_s)} 个"
          + ("  ✅ 干净" if not left_b and not left_s else "  ⚠️ 仍有残留"))


if __name__ == "__main__":
    main()
