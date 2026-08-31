"""钉死「不许把浏览器窗口移到屏幕外」。

2026-08-31 实测：`--window-position=-32000,-32000` 会让整条刷新链路失效。
挑战 JS 把窗口坐标当环境指纹，负的大数被判异常，平台既不给可自解的挑战、
也不给可点的验证页，而是把请求**推到登录页**（`/web/user/?seed=...`，7342 字节）。

对照实验（同出口、同批岗位、同一段 EXTRACT_JS，各自全新 browser）：
    在屏         → 4/4 ok，落岗位页
    离屏 -32000  → 0/4，落登录页

这个参数看着只是"别让窗口挡着人"的美化项，实际是链路的开关。它让我把故障
误判成风控，先后归因给"重启压塌 stoken 窗口"、"IP 被限制到明早 04:56"、
"SSH 隧道死了"、"冷启动漏了 REWARM_WAIT"——全错，浪费约 8 小时，
还让用户白点了两轮根本点不动的验证窗口。

判据：**反爬链路里，任何改变浏览器可见性/几何的参数都可能改变环境指纹。**
它们不是无害的美化项，要和 UA、时区、WebGL 一样当作指纹面处理。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 会把窗口挪出可见区域或改变几何的参数。
# `--headless` 不在这里：链路已有注释说明无头下挑战不自解，
# 而且它由 headless= 参数控制，另有其判据。
FORBIDDEN = (
    "--window-position=-",      # 负坐标 = 移出屏幕
    "--window-size=0",          # 零尺寸
    "--start-minimized",        # 最小化
)

# 需要检查的文件：所有会真的跑批、依赖挑战自解的脚本。
# 探针不列入——它们本来就是拿来做对照实验的，可以故意用坏配置。
TARGETS = (
    "scripts/refresh_status_loop.py",
    "scripts/manual_verify_proxy.py",
    "src/job_radar/collectors/browser.py",
)


def test_no_offscreen_window_args():
    bad: list[str] = []
    for rel in TARGETS:
        path = ROOT / rel
        if not path.exists():
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue          # 注释里提到它是允许的（现在就有一大段解释）
            for pat in FORBIDDEN:
                if pat in stripped:
                    bad.append(f"{rel}:{lineno}: {stripped[:70]}")
    assert not bad, (
        "这些参数会改变环境指纹、让挑战被推去登录页（2026-08-31 实测 0/4）：\n  "
        + "\n  ".join(bad))


def test_refresh_loop_stays_headed():
    """刷新链路必须有头：无头下挑战不自解（文件里早有注释，这里钉住）。"""
    src = (ROOT / "scripts" / "refresh_status_loop.py").read_text(encoding="utf-8")
    assert "headless=False" in src, "刷新链路必须 headless=False"
    # 允许注释里出现 headless=True 的讨论，但不能有真的赋值
    real = [ln.strip() for ln in src.splitlines()
            if "headless=True" in ln and not ln.strip().startswith("#")]
    assert not real, f"刷新链路不能用无头模式: {real}"
