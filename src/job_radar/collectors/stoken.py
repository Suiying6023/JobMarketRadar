"""兼容层：实现已迁到 `boss_scrape.stoken`，这里只做转出。

为什么合并：滚动 stoken 的算法和 DOM 邮筒的细节只有一份真源才安全。
两边各维护一份，改了一边忘了另一边，症状是"一边能跑一边 37"，
而且因为不报错，排查起来极其费时（实测：patchright 下读 `window.ABC`
永远是 `undefined`，脚本其实跑了）。

新代码请直接 `from boss_scrape.stoken import StokenRoller`。
"""

from __future__ import annotations

from boss_scrape.stoken import (  # noqa: F401 - 转出给旧调用点
    DATA_PREFIX,
    NAME_COOKIE,
    SECURITY_JS_PATH,
    SEED_COOKIE,
    SEED_COOKIES,
    STOKEN_COOKIE,
    STOKEN_TTL_SECONDS,
    TS_COOKIE,
    RollResult,
    StokenRoller,
    roll_quietly,
)

__all__ = [
    "StokenRoller", "RollResult", "roll_quietly",
    "STOKEN_COOKIE", "SEED_COOKIE", "NAME_COOKIE", "TS_COOKIE", "SEED_COOKIES",
    "STOKEN_TTL_SECONDS", "SECURITY_JS_PATH", "DATA_PREFIX",
]
