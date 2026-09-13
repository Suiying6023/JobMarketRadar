"""Configured request spacing and batch waits for managed browser operations."""
import asyncio

from .throttle import gaussian


class RequestPacer:
    def __init__(self, log=None):
        self.requests = 0
        self.details = 0
        self._lock = asyncio.Lock()
        self.log = log

    async def before(self, kind, config):
        async with self._lock:
            delay = 0.0
            extra = False
            if self.requests:
                delay += gaussian(*(config.detail_delay_seconds if kind == "detail" else config.request_delay_seconds))
                if self.requests % config.burst_threshold == 0:
                    delay += gaussian(*config.burst_penalty_seconds)
                    extra = True
                if kind == "detail" and self.details and self.details % config.detail_batch == 0:
                    delay += gaussian(*config.detail_cooldown_seconds)
                    extra = True
            if extra and self.log:
                self.log(f"按配置等待 {delay:.1f} 秒", kind="pacing")
            if delay:
                await asyncio.sleep(delay)
            self.requests += 1
            if kind == "detail":
                self.details += 1
