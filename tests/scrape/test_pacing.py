"""节奏三层：对数正态基础间隔 + 内容停留 + 挑战自适应。

三层各自的作用不同，混起来测会看不出是哪一层坏了：
  · 基础间隔决定**速率**（对数正态，重尾右偏）
  · 内容停留只微调形状（Nielsen 实测：用户平均只读网页 20% 的文字）
  · 挑战自适应只单向放慢（参考 guise_pacing 的 challenge_mult，上限 16×）
"""
from __future__ import annotations

import asyncio

import pytest

from job_radar.pacing import (
    MAX_CHALLENGE_MULTIPLIER,
    READING_MAX,
    READING_MIN,
    RequestPacer,
)
from job_radar.throttle import lognormal_pause


class Cfg:
    request_delay_seconds = (6.0, 11.0)
    detail_delay_seconds = (6.0, 11.0)
    burst_threshold = 20
    burst_penalty_seconds = (45.0, 75.0)
    detail_batch = 8
    detail_cooldown_seconds = (42.0, 62.0)


def collect_delays(n: int, setup=None) -> list[float]:
    """跑 n 次 before('detail')，把每次的 sleep 时长收集下来（不真的睡）。"""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake(delay, *a, **kw):
        recorded.append(delay)

    async def run():
        pacer = RequestPacer()
        if setup:
            setup(pacer)
        for _ in range(n):
            await pacer.before("detail", Cfg())

    asyncio.sleep = fake
    try:
        asyncio.run(run())
    finally:
        asyncio.sleep = real_sleep
    return recorded


# ---------- 基础间隔 ----------

def test_lognormal_floor_and_median():
    samples = sorted(lognormal_pause(6.0, 11.0) for _ in range(6000))
    assert samples[0] >= 6.0
    assert abs(samples[len(samples) // 2] - 8.12) < 0.6


def test_lognormal_is_heavy_tailed_but_capped():
    samples = sorted(lognormal_pause(6.0, 11.0) for _ in range(6000))
    assert samples[int(len(samples) * 0.95)] > 11.0, "p95 应超过配置上界"
    assert samples[-1] <= 11.0 * 8


# ---------- 内容停留 ----------

def test_short_content_speeds_up_long_content_slows_down():
    """停留系数只该微调：短 JD 略快、长 JD 略慢。

    直接断言系数而不是抽样求和 —— 重尾分布下 6 个样本的方差能把比值拉到 2 倍以上，
    那种测试测的是运气不是逻辑。
    """
    short = RequestPacer()
    short.signal("content", 0)
    long = RequestPacer()
    long.signal("content", 4000)
    assert short._reading == pytest.approx(READING_MIN)
    assert long._reading == pytest.approx(READING_MAX)
    assert READING_MAX / READING_MIN < 2.0, "跨度应温和，它只该是微调"


def test_content_factor_scales_monotonically():
    values = []
    for chars in (0, 500, 1500, 3000, 4000):
        pacer = RequestPacer()
        pacer.signal("content", chars)
        values.append(pacer._reading)
    assert values == sorted(values), "字数越多停留系数应单调不减"


def test_content_cap_stops_growing_beyond_limit():
    """超过 READING_CAP_CHARS 之后不再增加 —— 长 JD 的边际阅读量递减。"""
    at_cap = RequestPacer()
    at_cap.signal("content", 4000)
    over_cap = RequestPacer()
    over_cap.signal("content", 50000)
    assert at_cap._reading == over_cap._reading


# ---------- 挑战自适应 ----------

def test_challenge_multiplier_grows_then_decays():
    pacer = RequestPacer()
    pacer.signal("challenge")
    first = pacer._challenge
    pacer.signal("challenge")
    assert pacer._challenge > first, "连续挑战应继续放慢"
    pacer.signal("ok")
    assert pacer._challenge < first * 2, "成功后应开始恢复"


def test_challenge_multiplier_is_capped():
    """上限是硬约束：不封顶的话一个 8 秒间隔能放大成两分钟。"""
    pacer = RequestPacer()
    for _ in range(20):
        pacer.signal("challenge")
    assert pacer._challenge == MAX_CHALLENGE_MULTIPLIER


def test_challenge_slows_the_whole_interval():
    """用足够大的样本比均值 —— 小样本在重尾分布上没有判别力。"""
    def mean_of(delays):
        return sum(delays[1:]) / max(1, len(delays) - 1)

    normal = mean_of(collect_delays(400))
    slowed = mean_of(collect_delays(400, lambda p: p.signal("challenge")))
    assert slowed > normal * 1.5, f"挑战命中后间隔应显著变长（{normal:.1f} → {slowed:.1f}）"


def test_signal_never_raises_on_garbage():
    """信号只是优化，任何脏输入都不该影响采集。"""
    pacer = RequestPacer()
    for bad in (None, "", "abc", -1, object(), [1, 2]):
        pacer.signal("content", bad)
        pacer.signal(bad, bad)
        pacer.signal("unknown", 1)
    assert READING_MIN <= pacer._reading <= READING_MAX
