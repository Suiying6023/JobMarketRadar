"""钉死「每个暖 stoken 的地方都要等生效」。

2026-08-31 实测踩到的 bug：`warm_stoken` 只保证 cookie **出现**，
不保证已生效——实测「重算后等多久才生效」是 0s→0%、1s→33%、5s→100%
（`REWARM_WAIT` 常量注释里的原始数据）。

重暖路径一直有这个等待，但**冷启动和换出口路径漏了**。后果不是"偶尔失败"，
而是自我放大的死循环：第一条必撞墙 → window 收一格、strikes+1 →
三次就换出口 → 换出口的 warm 同样没等待 → 再撞。整批 0 ok / 100% challenged。

代价：这个现象被我误判成"两个 IP 都被风控"，弹了两轮无效的人工验证窗口，
让用户白等约 8 小时，还错怪用户"点错了登录页"。
而同一段 EXTRACT_JS 在探针里 4/4 成功——因为探针暖场后天然有间隔。

判据：**「拿到凭据」和「凭据可用」是两件事。** 任何"取得凭据 → 立刻使用"的
代码路径都要问一句：中间需要等生效吗？需要的话，**每一条**路径都得等，
漏一条就够形成上面那种循环。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "refresh_status_loop.py"


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _warm_calls_with_following_sleep() -> list[tuple[int, bool]]:
    """找出所有 warm_stoken 调用，判断其后是否紧跟 REWARM_WAIT 等待。

    用 AST 而不是正则：正则数不清嵌套与分支，而这里要的正是
    "每个调用点"这种结构信息。判定范围取该调用所在语句之后的
    同级若干条语句（暖完通常紧接着 log_requests / sleep）。
    """
    src = SRC.read_text(encoding="utf-8")
    lines = src.splitlines()
    out: list[tuple[int, bool]] = []
    for i, line in enumerate(lines):
        if "warm_stoken(" not in line or "async def" in line:
            continue
        if "REWARM_WAIT" in line:
            out.append((i + 1, True))
            continue
        # 往后看 12 行内是否出现 REWARM_WAIT 的 sleep。
        # 12 行足够覆盖 log_requests + 注释块，又不会跨到下一个逻辑段。
        window = "\n".join(lines[i + 1: i + 13])
        out.append((i + 1, "REWARM_WAIT" in window))
    return out


def test_every_warm_is_followed_by_wait():
    """每一处 warm_stoken 之后都必须有 REWARM_WAIT 等待。

    允许的例外：`if not await warm_stoken(...)` 这种**失败分支**
    （失败后要么换出口要么退出，不会紧接着 fetch）。
    """
    src_lines = SRC.read_text(encoding="utf-8").splitlines()
    missing = []
    for lineno, has_wait in _warm_calls_with_following_sleep():
        line = src_lines[lineno - 1]
        # 失败分支：`if not await warm_stoken(` —— 后面走的是移出出口池/break
        if "not await warm_stoken" in line:
            continue
        if not has_wait:
            missing.append(f"{lineno}: {line.strip()[:70]}")
    assert not missing, (
        "这些 warm_stoken 调用后面缺少 REWARM_WAIT 等待，"
        "会让紧接着的 fetch 必撞挑战页：\n  " + "\n  ".join(missing))


def test_rewarm_wait_lower_bound_is_at_least_five_seconds():
    """`REWARM_WAIT` 下界不能低于 5s。

    实测生效率：0s→0%、1s→33%、2s→100%、3s→67%、5s→100%、8s→100%。
    2s 看着够，但 n=3 时单次翻转就能造出那种反序——可信的只有单调趋势
    （0s 必失败、≥5s 必成功），所以取 5s。
    """
    tree = _tree()
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "REWARM_WAIT":
                    found = ast.literal_eval(node.value)
    assert found is not None, "REWARM_WAIT 常量不见了"
    assert found[0] >= 5.0, f"REWARM_WAIT 下界 {found[0]}s < 5s，生效率不可靠"
    assert found[1] >= found[0], "区间写反了"
