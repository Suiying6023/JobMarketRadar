"""请求间隔的生成器。实际节流由 `pacing.RequestPacer` 执行，这里只提供取数。

`lognormal_pause()` 是当前在用的那一个（重尾右偏）；`gaussian()` 只在 SSR 详情的
兜底 sleep 里用。

Timing values are operator configuration, not platform guarantees.
"""
from __future__ import annotations

import math
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


# 拟人间隔：**对数正态**，不是裁剪正态、也不是手工混合分布。
#
# 为什么换成对数正态（2026-10-19，参考开源实现后重写）：
#   · 人类行为间隔的实测形态是**重尾右偏**：多数很短，少数很长。
#     对数正态两个参数就能刻画，而且中位数可解释（第 50 百分位）。
#   · 参考 `tr4m0ryp/Linkedin_Automation` 的 `humanizer/delay.rs`：同样用
#     `mu = ln(median)` + sigma 控制尾部，并把样本 clamp 到 [min, max]，
#     理由是"单次抽到极端值不该让整轮停摆"。
#   · 上一版是 80/15/5 的手工混合。它虽然也有长尾，但**参数不可解释**：
#     "长停 5%、倍率 2.6–4.6" 是为了凑均值反推出来的，改一个就得重算三个。
#     对数正态只有一个形状参数 sigma，尾部重量直接由 `hi/lo` 决定。
#
# 参数映射（保持配置格式仍是 `[lo, hi]` 区间，不用引入新键）：
#   median = sqrt(lo * hi)          —— 几何中点
#   sigma  = ln(hi / lo) / 2        —— 让 lo/hi 大致落在 ±2σ 上
# 对 `[6, 11]`：median 8.12、sigma 0.303、均值 8.50（正好是区间中点）、
# p95 ≈ 13.4s、clamp 上限 8×hi = 88s。
_CLAMP_FACTOR = 8.0      # 上限 = hi × 8：截断极端抽样，避免单次抽到几分钟


def lognormal_pause(lo: float, hi: float) -> float:
    """在 `[lo, hi]` 上生成**对数正态**间隔：中位数是几何中点，右偏带长尾。

    和 `gaussian` 的区别：那个是对称的、裁剪在区间内、**永远没有长尾**——
    上界就是 hi，一眼能看出是机器。这个下界严格等于 lo、上界放宽到 `8×hi`，
    中间按对数正态分布。
    """
    if lo > hi:
        lo, hi = hi, lo
    if lo <= 0:
        return max(0.0, hi)
    median = math.sqrt(lo * hi)
    sigma = math.log(hi / lo) / 2
    if sigma <= 0:
        return median
    value = median * math.exp(random.gauss(0.0, sigma))
    return min(max(value, lo), hi * _CLAMP_FACTOR)


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
