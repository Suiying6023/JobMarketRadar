"""Legacy pacing helpers; managed tasks use pacing.RequestPacer.

Timing values are operator configuration, not platform guarantees.
"""
from __future__ import annotations

import asyncio
import random

from job_radar.models import CollectionConfig


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


class HumanThrottle:
    def __init__(
        self,
        config: CollectionConfig,
        detail_batch: int | None = None,
        detail_cooldown: tuple[float, float] | None = None,
        burst_threshold: int | None = None,
        burst_penalty: tuple[float, float] | None = None,
    ):
        """节流参数默认从 config 读（可按账号信任度在 yaml 里调档），
        显式传参仍可覆盖（backfill_details 用更短的批间冷却）。"""
        self.config = config
        self.detail_batch = detail_batch if detail_batch is not None else config.detail_batch
        self.detail_cooldown = detail_cooldown or config.detail_cooldown_seconds
        self.burst_threshold = (
            burst_threshold if burst_threshold is not None else config.burst_threshold
        )
        self.burst_penalty = burst_penalty or config.burst_penalty_seconds
        self._list_count = 0
        self._detail_count = 0

    def _gaussian(self, lo: float, hi: float) -> float:
        return gaussian(lo, hi)

    async def delay_before_request(self, kind: str) -> None:
        """请求前延迟。kind: list / detail。

        突发惩罚是在常规延迟**之上叠加**，不是替代。原先命中惩罚时 `return`
        跳过了常规延迟，若把 burst_penalty 配得比 delay 区间还短（惩罚 60-120s
        vs 详情延迟 8-12s 目前没问题，但列表延迟若配到 >60s 就会反转），
        第 N 条请求反而等得比平常更少——惩罚变成了奖励。叠加写法没有这个反转。
        """
        if kind == "detail":
            lo, hi = self.config.detail_delay_seconds
            self._detail_count += 1
            burst = self._detail_count >= self.burst_threshold
            if burst:
                self._detail_count = 0
        else:
            lo, hi = self.config.request_delay_seconds
            self._list_count += 1
            burst = self._list_count >= self.burst_threshold
            if burst:
                self._list_count = 0
        await asyncio.sleep(self._gaussian(lo, hi))
        if burst:
            p_lo, p_hi = self.burst_penalty
            await asyncio.sleep(self._gaussian(p_lo, p_hi))

    async def human_heartbeat(self, page, duration: float) -> None:
        """批间冷却期间模拟真人活动，保鲜 cookie（2026-08-02 调研方案）。

        BOSS 会对静置过久的会话判 cookie 失效，批间休息时长 3-5 分钟，
        期间做几次随机滚动/小幅停顿，模拟真实用户在页面浏览，让 cookie 不被判死。
        """
        import random as _random

        end = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < end:
            try:
                await page.evaluate("() => window.scrollBy(0, arguments[0])",
                                    _random.randint(100, 500))
            except Exception:
                pass
            await asyncio.sleep(_random.uniform(2.0, 5.0))
