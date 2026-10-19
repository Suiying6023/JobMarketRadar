"""Explicit offline network dependencies for parser/ledger unit tests."""
from job_radar.safety import AccessBlocked, AccessState
from job_radar.storage import JobStore


class PageEventRecorder:
    """Playwright `Page` 的替身基类：只实现「与业务无关、但生产代码一定会调」的那一层。

    起因是一个真实的漂移：`BossCollector.start()` 里挂 `page.on("response", ...)`、
    `close()` 里 `page.remove_listener(...)`、`_settle_on_origin()` 里查 `page.is_closed()`，
    而四个测试文件各自写的 FakePage 只覆盖了 evaluate / goto / wait_for_timeout。
    生产代码往前走了一步，替身没跟上，**60 个测试全部 AttributeError**；因为没人跑全量
    测试，这个红灯一直亮着（`一直亮的红灯等于没有红灯`）。

    这里做两件事：
    1. 把事件登记、关闭状态这类通用接口一次性实现掉，子类不必重复；
    2. 用 `__getattr__` 把「替身没实现某个 Page 方法」变成一条能直接照着修的错误信息，
       而不是一句光秃秃的 `AttributeError: 'FakePage' object has no attribute 'xxx'`。

    仍然只做登记、不伪造事件；要模拟响应，测试自己调 `emit()`。
    用 `__dict__` 惰性建表，这样子类不改 `__init__` 也能继承。
    """

    def on(self, event: str, handler) -> None:
        self.__dict__.setdefault("_page_listeners", {}).setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        handlers = self.__dict__.get("_page_listeners", {}).get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event: str, payload=None) -> None:
        for handler in list(self.__dict__.get("_page_listeners", {}).get(event, [])):
            handler(payload)

    def listener_count(self, event: str) -> int:
        return len(self.__dict__.get("_page_listeners", {}).get(event, []))

    def is_closed(self) -> bool:
        return bool(self.__dict__.get("_page_closed", False))

    async def close(self) -> None:
        self.__dict__["_page_closed"] = True

    def __getattr__(self, name: str):
        # 只在正常查找失败时才会走到这里，所以不会掩盖子类自己的属性。
        raise AttributeError(
            f"测试替身没有实现 Playwright Page 的 `{name}`。"
            f"这说明采集器（src/job_radar/collectors/boss.py）用到了新的 Page 接口，"
            f"而 tests/fakes.py::PageEventRecorder 没跟上。"
            f"请在该类里补一个最小实现，不要在测试里用 MagicMock 糊过去。"
        )


class MemoryGate:
    def __init__(self):
        self.state = AccessState(mode="ready", manual_release_required=False)

    def read_state(self):
        return self.state

    def require_online(self, enabled=True):
        if not enabled or self.state.mode != "ready":
            raise AccessBlocked("offline test gate")
        return self.state

    def pause(self, reason, resume_after=None, source="test"):
        self.state = AccessState(mode="paused", reason=reason, manual_release_required=True)
        return self.state


def configure_offline_collector(collector, ledger=None):
    # Production never guesses whether a Page is fake. Tests supply every dependency.
    collector.config.network_enabled = True
    collector.gate = MemoryGate()
    collector.ledger = ledger if ledger is not None else JobStore(":memory:")
    return collector
