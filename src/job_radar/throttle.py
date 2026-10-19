"""请求间隔的生成器。实际节流由 `pacing.RequestPacer` 执行，这里只提供取数。

`human_pause()` 是当前在用的那一个；`gaussian()` 只在 SSR 详情的兜底 sleep 里用。

Timing values are operator configuration, not platform guarantees.
"""
from __future__ import annotations

import random


def gaussian(lo: float, hi: float) -> float:
    """在 [lo, hi] 内生成高斯分布延迟（裁剪到区间内）。比 uniform 更接近真人时序。

    区间写反（yaml 里配成 [12, 8]）时原先会死循环——采样永远落不进空区间，
    整轮采集静默卡死。这里先归正区间，配错也只是延迟顺序无所谓。
    """
    if lo > hi:
        lo, hi = hi, lo
    mean = (lo + hi) / 2
    std = (hi - lo) / 4  # 让 ±2σ 覆盖区间
    if std <= 0:
        return lo
    for _ in range(64):
        v = random.gauss(mean, std)
        if lo <= v <= hi:
            return v
    return mean  # 极端情况兜底，不让调用方卡住


# 拟人抖动的混合分布：三档，权重 + 在**以 d=(hi-lo) 为单位、从 lo 起算**的区间。
#
# 为什么不用 `gaussian`：裁剪到 [lo, hi] 的对称正态**永远不会走神**——没有长尾，
# 最大也就 hi。真人不是这样：多数时候手快，偶尔停下来看正文、回消息、被别的事
# 打断。所以改成三档混合。
#
# ⚠️ **下限必须严格等于 lo**。第一版用"相对中点的倍数"，结果 [6,11] 会抽出
# 5.04 秒——比运维设的 6 秒地板还快。均值虽然守住了，但配置的语义被悄悄改掉了：
# 写 [6,11] 就该真的不短于 6 秒。速率是唯一实测过安全区间的量，地板更是。
#
# 三档（d = hi - lo，全部从 lo 起算）：
#   · 常态  80%  [0.00, 0.45]d —— 手快
#   · 走神  15%  [0.45, 1.00]d —— 看正文
#   · 长停   5%  [1.00, 7.00]d —— 被别的事打断
#
# 加权均值 = lo + 0.48875·d，而区间中点 = lo + 0.5·d —— **相差不到 1.2% 的 d**，
# 所以不需要任何归一化：均值天然落在中点附近，下限天然就是 lo，全程无状态。
_HUMAN_MIX = ((0.80, 0.00, 0.45), (0.15, 0.45, 1.00), (0.05, 1.00, 7.00))


def human_pause(lo: float, hi: float) -> float:
    """在 `[lo, hi]` 上生成拟人延迟：**不短于 lo**，均值≈区间中点，右偏带长尾。

    和 `gaussian` 的区别：均值几乎相同，但分布右偏且有 5% 的长停，不像节拍器；
    而且上不封顶（最长的走神可以到 `lo + 7d`），下严格以 `lo` 为地板。
    区间写反时同样先归正（见 `gaussian` 的说明）。
    """
    if lo > hi:
        lo, hi = hi, lo
    span = hi - lo
    if span <= 0:
        return lo
    roll = random.random()
    acc = 0.0
    for weight, lower, upper in _HUMAN_MIX:
        acc += weight
        if roll <= acc:
            return lo + random.uniform(lower, upper) * span
    return lo + 0.5 * span


# 2026-10-19 删除 `HumanThrottle` 与 `human_heartbeat`：两者在取消请求账本的重构
# （425e513）之后就**没有任何调用点**了，只剩 `tests/_legacy/` 里的旧测试引用。
# 实际在跑的是 `pacing.RequestPacer`。
#
# 唯一值得记住、别丢的知识点来自 `human_heartbeat` 的原文档：
#   「BOSS 会对静置过久的会话判 cookie 失效，批间休息 3-5 分钟时需要模拟
#     页面滚动来保鲜。」
# 之所以没有把它接回来，是因为它靠 `window.scrollBy` 活动页面 ——
# 如果那一刻页面停在推荐流/搜索结果上，滚动会**触发下一页请求**，等于凭空多发
# 一个我们没算过的请求（同类坑见 session.py 里"先翻页后抓详情"的说明）。
# 而且我们现在的批间冷却只有 40-60 秒，够不上"静置判死"的门槛。
# 将来若要把单次休息拉长到分钟级，再实现时必须先加"只在详情页滚动"的守卫。
