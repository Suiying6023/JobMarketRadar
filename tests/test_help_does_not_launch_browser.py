"""会开浏览器窗口的脚本必须先解析参数，否则 `--help` 也会弹窗。

踩过一次：`check_account_status.py` 当时没有 argparse，于是批量核对
「每个脚本 `--help` 能不能跑」时，它把 `--help` 当普通参数忽略、
直接开了一个可见浏览器并按设计等 10 分钟 ——
**屏幕上出现一个「安全验证」窗口，看起来完全像风控事件**，
而它只是一次无害的自检动作造成的。

判据：**任何有副作用的脚本，副作用都必须发生在参数解析之后。**
`--help` / 参数错误应当在什么都没做的情况下退出。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

LAUNCH_CALLS = ("chromium.launch", "launch_persistent_context")


def _browser_scripts() -> list[Path]:
    """会起浏览器的脚本。探针不算 —— 它们是一次性实验，本来就要手动跑。"""
    out = []
    for p in sorted((ROOT / "scripts").glob("*.py")):
        if p.name.startswith(("_", "probe_")):
            continue
        text = p.read_text(encoding="utf-8")
        if any(c in text for c in LAUNCH_CALLS):
            out.append(p)
    return out


def test_there_are_browser_scripts_to_check():
    """守住这个测试自己：选择器失效时应当失败，而不是安静地零覆盖。"""
    assert _browser_scripts(), "一个都没匹配到，说明 LAUNCH_CALLS 或路径过滤失效了"


@pytest.mark.parametrize("path", _browser_scripts(), ids=lambda p: p.name)
def test_browser_script_parses_args(path: Path):
    """必须调用 `parse_args()`，哪怕一个参数都不需要。

    ⚠️ 判据只查"有没有"，**不比行号先后**：`launch` 常写在某个被调用的函数体里，
    而 `parse_args` 在 `main()` 里 —— 源码里 launch 在前是正常的，
    定义顺序不等于执行顺序。想验执行顺序得做调用图分析，
    而真正要防的是"完全没有 argparse"那一种（那才是必然弹窗）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    has_parse = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "parse_args"
        for n in ast.walk(tree))
    assert has_parse, (
        f"{path.name} 会开浏览器却没有 parse_args() —— "
        "`--help` 会被当普通参数忽略，于是自检动作本身就弹出了窗口")
