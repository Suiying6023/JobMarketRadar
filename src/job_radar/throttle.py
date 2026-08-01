"""人类行为节流模块：复用参考项目的风控规避设计。

参考来源（2026-08-01 调研）：
- Auto-JobHunter：高斯抖动 + 突发惩罚节流
- boss-zhipin-scraper：全局请求预算 + 退避
- boss-agent-cli：高斯节流 + 风控即停

设计要点：
1. 请求间隔用高斯分布（mean ± std），比 uniform 更接近真实人操作的时序——
   真实用户有时快有时慢，不是均匀的。
2. 突发惩罚：连续 N 条请求后强制插入一次较长冷却，避免「稳定高速」的机器特征。
3. 批次冷却：详情接口有账号级累计配额（约 5-6 次触发 code=37），
   每批后必须长休息等窗口恢复，这是唯一有效防线。
4. 全局预算：单轮请求上限，超了就停。

用法：
    throttle = HumanThrottle(detail_batch=5, detail_cooldown=(1200, 1800))
    await throttle.delay_before_request('list')   # 列表请求前
    await throttle.delay_before_request('detail') # 详情请求前
    await throttle.after_batch('detail')          # 每批详情后冷却
"""
from __future__ import annotations

import asyncio
import random

from job_radar.models import CollectionConfig


class HumanThrottle:
    def __init__(
        self,
        config: CollectionConfig,
        detail_batch: int = 5,
        detail_cooldown: tuple[float, float] = (900.0, 1500.0),
        burst_threshold: int = 8,
        burst_penalty: tuple[float, float] = (60.0, 120.0),
    ):
        self.config = config
        self.detail_batch = detail_batch        # 详情接口每批条数
        self.detail_cooldown = detail_cooldown  # 详情批间冷却（秒）
        self.burst_threshold = burst_threshold  # 连续多少条触发突发惩罚
        self.burst_penalty = burst_penalty      # 突发惩罚时长（秒）
        self._list_count = 0
        self._detail_count = 0

    def _gaussian(self, lo: float, hi: float) -> float:
        """在 [lo, hi] 内生成高斯分布延迟（裁剪到区间内）。"""
        mean = (lo + hi) / 2
        std = (hi - lo) / 4  # 让 ±2σ 覆盖区间
        while True:
            v = random.gauss(mean, std)
            if lo <= v <= hi:
                return v

    async def delay_before_request(self, kind: str) -> None:
        """请求前延迟。kind: list / detail。"""
        if kind == "detail":
            lo, hi = self.config.detail_delay_seconds
            self._detail_count += 1
            if self._detail_count >= self.burst_threshold:
                # 突发惩罚：连续请求太多，强制冷却
                self._detail_count = 0
                p_lo, p_hi = self.burst_penalty
                wait = self._gaussian(p_lo, p_hi)
                await asyncio.sleep(wait)
                return
        else:
            lo, hi = self.config.request_delay_seconds
            self._list_count += 1
            if self._list_count >= self.burst_threshold:
                self._list_count = 0
                p_lo, p_hi = self.burst_penalty
                await asyncio.sleep(self._gaussian(p_lo, p_hi))
                return
        await asyncio.sleep(self._gaussian(lo, hi))

    async def after_detail_batch(self) -> None:
        """每批详情请求结束后长冷却（解决详情接口累计配额）。"""
        self._detail_count = 0
        lo, hi = self.detail_cooldown
        await asyncio.sleep(self._gaussian(lo, hi))

    def in_detail_batch(self, current: int) -> bool:
        """判断是否仍在当前详情批次内（用于决定是否触发批间冷却）。"""
        return current % self.detail_batch != 0
