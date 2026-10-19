"""BOSS 直聘抓取层：只负责把数据拿回来。

刻意与 `job_radar` 解耦——不碰数据库、不碰前端、不碰访问策略文件。
调用方拿到 `JobCard` / `JobDetail` 之后想存哪里、怎么筛，都与这里无关。

## 为什么这样设计（都是实测结论，不是偏好）

1. **续窗口靠算 token，不靠重新打开页面。**
   同一个 `__zp_stoken__` 只允许 5 个请求，第 6 个回 `code=37`。
   但每个 `code=0` 响应都会下发新种子（`__zp_sseed__` / `__zp_sname__` /
   `__zp_sts__`），就地能算出新 token，**成本 0 个请求**。
   整页导航也能续，但一次导航实测 92~993 个真实请求。

2. **`code=37` 是设计内的轮换，不是封禁。**
   撞到就换 token 重试一次；重试还不行才是真异常。

3. **`code=36` 是"要求人工过验证"，等待无效。**
   实测等 12 分钟、整页重载都清不掉；人点一下验证立刻恢复。

4. **`code=32/35` 才是封禁。**
   历史记录：`code=32` 封了整 72 小时，诱因是深翻到第 40 页 + 连续 8 次重算 token。

## 典型用法

    async with BossClient(profile=".browser-profile") as client:
        async for card in client.search("AI Agent", city="101210100", pages=3):
            detail = await client.detail(card)
"""

from .client import BossClient, SearchSpec
from .errors import (
    AccountRestricted,
    BossScrapeError,
    LoginExpired,
    RiskControl,
    Throttled,
    VerificationRequired,
)
from .models import JobCard, JobDetail

__all__ = [
    "BossClient",
    "SearchSpec",
    "JobCard",
    "JobDetail",
    "BossScrapeError",
    "AccountRestricted",
    "LoginExpired",
    "RiskControl",
    "Throttled",
    "VerificationRequired",
]
