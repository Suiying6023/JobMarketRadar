"""Configured request spacing and batch waits for managed browser operations."""
import asyncio

from .throttle import human_pause


class RequestPacer:
    def __init__(self, log=None):
        self.requests = 0
        self.details = 0
        self._lock = asyncio.Lock()
        self.log = log

    async def before(self, kind, config):
        """请求前等待。**用 `human_pause` 而不是固定间隔**：

        固定间隔（哪怕是带抖动的对称正态）本质上还是节拍器——每个间隔都落在
        同一个窄区间里。`human_pause` 均值不变、形状右偏且有长停，更接近真人
        时序。速率仍由配置的中点决定，所以"每小时多少条"依然可推算。
        """
        async with self._lock:
            delay = 0.0
            extra = False
            if self.requests:
                delay += human_pause(*(config.detail_delay_seconds if kind == "detail" else config.request_delay_seconds))
                if self.requests % config.burst_threshold == 0:
                    delay += human_pause(*config.burst_penalty_seconds)
                    extra = True
                if kind == "detail" and self.details and self.details % config.detail_batch == 0:
                    delay += human_pause(*config.detail_cooldown_seconds)
                    extra = True
            if extra and self.log:
                self.log(f"按配置等待 {delay:.1f} 秒", kind="pacing")
            if delay:
                await asyncio.sleep(delay)
            self.requests += 1
            if kind == "detail":
                self.details += 1
