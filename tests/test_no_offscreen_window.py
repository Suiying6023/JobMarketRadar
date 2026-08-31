"""浏览器窗口几何属于**环境指纹面**，负坐标会让整条刷新链路失效。

实测（同出口、同批岗位、同一段 EXTRACT_JS，各组全新 browser）：

| 窗口位置 | 结果 |
|---|---|
| `-32000,-32000` | **0/4**，请求被推去登录页（`/web/user/?seed=...`，7342 字节） |
| 在屏（不指定） | 4/4 ok，落岗位页 |
| `2400,1400` | 挑战率 **7%**（n=14），优于在屏基线 14-16% |

负坐标下平台既不给可自解的挑战、也不给可点的验证页 —— **那种验证窗口人是点不动的**。
所以禁的是**负坐标与零尺寸**，不是"移动窗口"本身：挑战 JS 不介意窗口在哪，
介意的是坐标像不像真实环境。这个区分是必要的，跑批窗口必须能移出主屏
（每 15-25 秒新建 context，反复抢焦点会把全屏应用踢出来）。

`--headless` 不在这里：无头下挑战不自解，由 `headless=` 参数控制，另有其判据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from job_radar.models import BrowserConfig
from job_radar.refresh_fetch import DEFAULT_WINDOW_POSITION, launch_args

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = (
    "--window-position=-",      # 负坐标：被判异常环境，请求落登录页（实测 0/4）
    "--window-size=0",          # 零尺寸
    "--start-minimized",        # 最小化（Windows 会把坐标写成 -21333，等价负坐标）
)

# 所有会真的跑批、依赖挑战自解的脚本。
# 探针不列入——它们本来就是拿来做对照实验的，可以故意用坏配置。
TARGETS = (
    "scripts/refresh_status_loop.py",
    "scripts/manual_verify_proxy.py",
    "scripts/check_job_status.py",
    "scripts/expand_list.py",
    "scripts/enrich_company_info.py",
    "src/job_radar/collectors/browser.py",
    "src/job_radar/refresh_fetch.py",
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
                continue          # 注释里说明它为什么有害是允许的
            for pat in FORBIDDEN:
                if pat in stripped:
                    bad.append(f"{rel}:{lineno}: {stripped[:70]}")
    assert not bad, (
        "这些参数会改变环境指纹、让挑战被推去登录页（实测 0/4）：\n  "
        + "\n  ".join(bad))


@pytest.mark.parametrize("pos", ["-32000,-32000", "-1,-1", "2400,-1400"])
def test_launch_args_rejects_negative_coordinates(pos):
    """负坐标在**运行时**就要拒绝，不能只靠静态检查。

    静态检查只看得见字面量，而窗口位置现在是参数（`--window-position`）——
    从命令行传进来的负坐标绕过上面那条测试。
    """
    with pytest.raises(ValueError, match="必须为正"):
        launch_args(pos)


def test_launch_args_default_is_offscreen_and_positive():
    args = launch_args()
    assert f"--window-position={DEFAULT_WINDOW_POSITION}" in args
    x, y = (int(v) for v in DEFAULT_WINDOW_POSITION.split(","))
    assert x > 0 and y > 0
    # 反检测参数不能被顺手删掉：它同时是 cleanup_browsers.py 认自家进程的特征。
    assert "--disable-blink-features=AutomationControlled" in args


def test_launch_args_empty_position_means_onscreen():
    """空字符串 = 不指定位置。给「要让人看见这个窗口」的场景用。"""
    assert launch_args("") == ["--disable-blink-features=AutomationControlled"]


def test_browser_config_rejects_negative_position():
    """配置层同样要拦：config YAML 里写负坐标应当在加载时就失败。"""
    with pytest.raises(ValueError):
        BrowserConfig(window_position="-32000,-32000")


def test_refresh_loop_stays_headed():
    """刷新链路必须有头：无头下挑战不自解。"""
    src = (ROOT / "scripts" / "refresh_status_loop.py").read_text(encoding="utf-8")
    assert "headless=False" in src, "刷新链路必须 headless=False"
    real = [ln.strip() for ln in src.splitlines()
            if "headless=True" in ln and not ln.strip().startswith("#")]
    assert not real, f"刷新链路不能用无头模式: {real}"
