"""钉死「验证是否通过」的判据：看风控 cookie，不看页面内容。

用户 2026-08-31 反馈：「我验证成功后一直重复加载验证组件是什么情况」。

原因：两处判据都把**两件独立的事**当成了一件
  ① 人过掉了验证（拿到 `__zp_stoken__`）
  ② 匿名访客能看到完整岗位页
这条链路是免登录的，②**本来就可能再次要求验证**。原判据是
  refresh_status_loop: `"招聘" in title and "注册登录" not in title`
  manual_verify_proxy: `"验证" not in title and len(body) > 500`
都属于②。于是人点完 → 脚本立刻重新导航 → 平台又要验证 → 判定"还没过"
→ 再导航（最多 6 次）→ 用户眼里就是验证组件反复加载，而且每次重开
都是一次真实请求，越撞越糟。

判据：**人的劳动在拿到风控 cookie 那一刻就完成了**，不该被之后
"能不能看到内容"这件独立的事否定。
更一般的形态：验收一个动作时，要检查**那个动作的直接产物**，
而不是检查依赖它的下游结果 —— 下游还有别的失败原因。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = (
    "scripts/refresh_status_loop.py",
    "scripts/manual_verify_proxy.py",
)


def _code_lines(rel: str) -> list[tuple[int, str]]:
    """只返回非注释的代码行（注释里讨论旧判据是允许的）。"""
    out = []
    for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append((i, s))
    return out


def test_verify_success_checks_risk_cookie():
    """两处等待验证的循环都必须以 __zp_stoken__ 作为通过判据。"""
    for rel in TARGETS:
        src = "\n".join(s for _, s in _code_lines(rel))
        assert "__zp_stoken__" in src, (
            f"{rel} 没有用风控 cookie 判验证通过 —— "
            "用标题/正文长度判会让人的成功点击被忽略")


def test_no_title_only_pass_criterion():
    """**等人过验证的那个循环**不许用「标题/正文」判通过。

    ⚠️ 范围限定在"人工验证等待循环"，不含页面状态分类。
    `probe_job_page` / `load_and_read` 里同样有 `"招聘" in title` 的判断，
    那两处是**给页面归类**（ok / verify / login / dead），不是验收人的动作 ——
    它们用标题是对的，不该被这个测试连坐。
    区分方式：只看紧跟着把"通过"标志置真的那几行。
    """
    bad = []
    for rel in TARGETS:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("#") or not s:
                continue
            # 往后看 3 行内是否把通过标志置真（ok = True / passed = True）
            window = "\n".join(lines[i: i + 4])
            sets_pass = ("ok = True" in window) or ("passed = True" in window)
            if not sets_pass:
                continue
            if ('"招聘" in t' in s and "not in t" in s) or "len(body) > 500" in s:
                bad.append(f"{rel}:{i+1}: {s[:70]}")
    assert not bad, (
        "这些判据把「能看到岗位页」当成「验证通过」，会让用户反复点验证：\n  "
        + "\n  ".join(bad))


def test_renav_attempts_are_bounded_small():
    """重新导航的次数要小。

    每次重开都是一次真实请求；原来是 6 次，在被限制期间等于连撞 6 下
    （而被限制期间每访问一次还会把解封时间重置为此刻+24h）。
    """
    src = (ROOT / "scripts" / "refresh_status_loop.py").read_text(encoding="utf-8")
    assert "renav < 2" in src, "重新导航次数应收紧到 2 次以内"
    assert "renav < 6" not in src, "6 次重开会在受限期间反复撞墙"
