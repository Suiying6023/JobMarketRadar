"""Configured request spacing and batch waits for managed browser operations.

节奏分三层，缺一层就会露出机器痕迹：

  1. **基础间隔**：对数正态（`throttle.lognormal_pause`）——重尾右偏，
     多数很短、少数很长。裁剪正态做不到这一点：它没有长尾。
  2. **内容相关停留**：读完一条 JD 之后多停一会儿，停留系数按字数算，
     但只按 **20%** 的字数算（Nielsen 实测：用户平均只读网页 20% 的文字），
     并且有上限 —— 否则一篇 5000 字的 JD 会把节奏拖垮。
  3. **挑战自适应**：被站点推回时放慢、恢复后衰减回来。
     参考 `guise_pacing::RequestPacer`（`MAX_CHALLENGE_MULTIPLIER = 16`），
     它的描述是"delay grows like a human who slows down when a site pushes back"。
"""
import asyncio

from .throttle import lognormal_pause

#: 挑战倍数上限。与 guise_pacing 的 `MAX_CHALLENGE_MULTIPLIER` 取同一个值：
#: 足够在连续撞墙时把节奏压到很慢，又不至于把一个 8 秒的间隔放大成十分钟。
MAX_CHALLENGE_MULTIPLIER = 16.0
#: 每次命中挑战时的放大系数；每次成功后的衰减系数。
#: 放大快、衰减慢 —— 被推回要立刻让路，恢复要谨慎，不能一成功就弹回原速。
CHALLENGE_GROWTH = 2.0
CHALLENGE_DECAY = 0.85

#: 内容相关停留系数的上下限。0.85 表示"这条很短，看完就下一个"；
#: 1.50 表示"这条很长，多停一会儿"。跨度刻意不大 —— 它只该是微调，
#: 速率主要由基础间隔和批间冷却决定。
READING_MIN = 0.85
READING_MAX = 1.50
#: 字数封顶：超过这个字数不再增加停留（长 JD 的边际阅读量递减）。
READING_CAP_CHARS = 4000


class RequestPacer:
    def __init__(self, log=None):
        self.requests = 0
        self.details = 0
        self._lock = asyncio.Lock()
        self.log = log
        self._reading = 1.0          # 内容相关停留系数
        self._challenge = 1.0        # 挑战自适应倍数
        self.challenge_hits = 0

    # ---------- 外部信号 ----------
    def signal(self, name, value=None):
        """采集器回传的观察信号。**同步、无锁、绝不抛异常** —— 它不该影响采集。

        `name`：
          · `"content"` —— value 是刚读到的 JD 字数，用于下一次间隔的停留系数
          · `"challenge"` —— 命中了挑战/风控信号，放慢
          · `"ok"` —— 请求成功，逐步恢复
        """
        try:
            if name == "content":
                chars = max(0, min(int(value or 0), READING_CAP_CHARS))
                ratio = chars / READING_CAP_CHARS
                self._reading = READING_MIN + (READING_MAX - READING_MIN) * ratio
            elif name == "challenge":
                self.challenge_hits += 1
                self._challenge = min(self._challenge * CHALLENGE_GROWTH,
                                      MAX_CHALLENGE_MULTIPLIER)
                if self.log and self._challenge > 1.0:
                    self.log(f"检测到挑战信号，节奏放慢到 {self._challenge:.1f}×",
                             kind="pacing")
            elif name == "ok":
                self._challenge = max(1.0, self._challenge * CHALLENGE_DECAY)
        except Exception:  # noqa: BLE001 - 信号只是优化，失败不影响采集
            pass

    # ---------- 主循环 ----------
    async def before(self, kind, config):
        """请求前等待。

        三层叠加：基础间隔（对数正态）+ 突发/批间冷却（同样对数正态）
        + 内容停留系数 + 挑战倍数。前三项决定**速率**，后两项只改变**形状**。
        """
        async with self._lock:
            delay = 0.0
            extra = False
            if self.requests:
                window = (config.detail_delay_seconds if kind == "detail"
                          else config.request_delay_seconds)
                delay += lognormal_pause(*window)
                if self.requests % config.burst_threshold == 0:
                    delay += lognormal_pause(*config.burst_penalty_seconds)
                    extra = True
                if kind == "detail" and self.details and self.details % config.detail_batch == 0:
                    delay += lognormal_pause(*config.detail_cooldown_seconds)
                    extra = True
                # 内容停留与挑战倍数只作用于**基础间隔之后**的整体，
                # 不放大批间冷却 —— 冷却本身就是"离开一会儿"，
                # 再乘一个倍数会让长尾失控。
                delay *= self._reading * self._challenge
            if (extra or self._challenge > 1.0) and self.log:
                self.log(f"按配置等待 {delay:.1f} 秒"
                         f"（停留 ×{self._reading:.2f}，挑战 ×{self._challenge:.2f}）",
                         kind="pacing")
            if delay:
                await asyncio.sleep(delay)
            self.requests += 1
            if kind == "detail":
                self.details += 1
