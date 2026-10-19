"""抓取层错误。分成"能自己处理"和"必须人来"两类。

分界线是实测出来的，不是猜的：

| 情况 | 谁处理 | 依据 |
|---|---|---|
| `code=37` 环境异常 | **代码自己** | 一个 token 只给 5 个请求，换 token 立刻恢复 |
| `code=0` + 访问频繁 | **代码自己** | 退避重试有效 |
| `code=36` 账户异常行为 | **必须人来** | 等 12 分钟无效，整页重载无效，只有人工过验证 |
| `code=32/35` | **必须人来** | 历史封禁 72 小时 |
| `code=7` 登录失效 | **必须人来** | 要重新扫码登录 |

把"必须人来"的情况当成"能自己处理"，就是原先那版最大的问题：
它把 37 当封禁停机，却把 36 当限速退避。
"""

from __future__ import annotations


class BossScrapeError(RuntimeError):
    """抓取层的基类。"""


class RiskControl(BossScrapeError):
    """平台风控响应，且不是已知可自动恢复的那几种。"""

    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TokenWindowExhausted(RiskControl):
    """`code=37`：这个 stoken 的 5 个额度用完了。

    正常情况下不该冒到调用方——`BossClient` 会自动换 token 重试一次。
    冒出来说明换了 token 还是被拒，那才是真异常。
    """


class Throttled(BossScrapeError):
    """`code=0` + 「访问频繁，请稍后再试」：限速，不是封号。退避重试有效。"""

    def __init__(self, message: str, *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class VerificationRequired(BossScrapeError):
    """`code=36`：平台要求人工过验证。

    实测（2026-10-18）：岗位页会被 302 到
    `https://www.zhipin.com/web/passport/zp/verify.html?code=36`，
    正文是「您的账号可能存在异常访问行为，完成验证后即可正常使用」。
    等 12 分钟不会自己消失，整页重载也清不掉，**只有人点一下能解**。

    调用方应该：把浏览器窗口推到前台（`BossClient.show_window()`），
    让人过完验证，然后重新开始。
    """


class AccountRestricted(BossScrapeError):
    """`code=32/35`：账号/IP 被封。

    历史记录（2026-09-12）：`code=32` 封了整 72 小时，到点自然解封。
    封禁期间**任何自动化请求都会延长风险**，必须完全静默。
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class LoginExpired(BossScrapeError):
    """`code=7`：登录态失效，需要重新登录。"""


# ---------------------------------------------------------------- 码表（唯一真源）

# 平台风控码。31 = IP 封禁、32 = 账号封禁、35 = IP 异常、36 = 要人过验证、37 = token 窗口耗尽。
# 采集链路（`job_radar.collectors.boss`）按它决定熔断与账本 outcome，
# 抓取层（`client.BossClient`）按它决定抛哪种异常 —— **两边读同一份**，
# 免得一边把 36 当限速退避、另一边当封禁。
RISK_CODES = frozenset({31, 32, 35, 36, 37})

# 登录态失效：HTTP 200 + code=7「当前登录状态已失效」。
#
# 这**不是**风控，不能进 RISK_CODES —— 进了就会把持久策略冻结，而正确处理是
# 「去浏览器里重新登录」，冻结反而挡住登录后继续跑。2026-10-18 真实线路实测：
# 上次登录是 9-30，18 天后 getUserInfo 直接回 code=7，导航本身 HTTP 200 正常。
AUTH_EXPIRED_CODES = frozenset({7})

# 关键字兜底：防止平台新增的风控码被误判成普通业务错误。
# 「操作太频繁」「滑块」来自参考项目，实测在 82 条正常详情响应里 0 命中，可安全加入。
# 参考项目还用了「验证」「安全校验」，但实测它们在正常 JD 里命中多次——全是正文里的
# 「测试验证」「效果验证」「数据安全校验」「安全校验通过」等，会把好数据判成风控，不采用。
# 真正的风控响应是 code=37/32/36（有独立判断）或「访问频繁」这类精确文案。
#
# ⚠️ 只查 `message` 字段、不扫全文：JD 正文里出现「滑块」的岗位是正常内容
# （爬虫岗、安全岗），扫全文会误杀好数据。
RISK_TEXTS = (
    "访问频繁",
    "环境存在异常",
    "账户存在异常",
    "操作过于频繁",
    "操作太频繁",
    "滑块",
)
