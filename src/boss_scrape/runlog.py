"""运行日志：每次真实抓取都留一份可分析的风控样本。

## 为什么需要它

风控规则是靠样本推出来的，而样本必须是**真实运行**里的，不是实验脚本里的。
这里每条记录都带够上下文，事后能直接回答这几个问题：

  · `code=37` 出现在第几个请求之后？（验证"一个 token 5 个请求"）
  · `code=36` 之前 M 秒内发了多少请求？（标定 L2 阈值）
  · 滚动失败是从哪一次开始的？之后是不是就退回导航了？
  · 换了出口 IP 之后，36 出现的频率变了没有？

所以字段里刻意带了 `since_start_s` 和 `requests`——只记 code 是没法算速率的。

## 格式

JSONL，一行一个事件。事件类型：

    start / request / roll / throttle / risk / stop / end

**不记录** token 值、种子值、cookie。这些是凭据，不是样本。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

MAX_BODY = 400


class RunLog:
    """写 JSONL。`path=None` 时静默丢弃（测试/离线用）。"""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._started = time.time()
        self.entries = 0
        self.last_error: str | None = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                self.path = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, event: str, **fields) -> None:
        if self.path is None:
            return
        now = time.time()
        record = {
            "t": round(now, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "since_start_s": round(now - self._started, 1),
            "event": event,
        }
        record.update(fields)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.entries += 1
        except OSError as exc:  # 日志写不了不能影响抓取
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            self.path = None


def _read(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(path: str | Path, *, window_s: float = 60.0) -> dict:
    """把一份日志压成可判断的结论。

    重点不是"发了多少请求"，而是**风控事件之前那段时间的速率**——
    阈值只能从速率反推，不能从累计量反推。
    """
    rows = _read(path)
    requests = [r for r in rows if r.get("event") == "request"]
    rolls = [r for r in rows if r.get("event") == "roll"]
    risks = [r for r in rows if r.get("event") == "risk"]
    throttles = [r for r in rows if r.get("event") == "throttle"]

    codes = Counter(str(r.get("code")) for r in requests)
    roll_reasons = Counter(str(r.get("reason")) for r in rolls)
    risk_codes = Counter(str(r.get("code")) for r in risks)

    # 每个风控事件往前 window_s 秒内的请求数：这是标定阈值的关键数字。
    rate_at_risk: list[dict] = []
    for risk in risks:
        end = float(risk.get("t") or 0)
        if not end:
            continue
        count = sum(1 for r in requests
                    if end - window_s <= float(r.get("t") or 0) <= end)
        rate_at_risk.append({"code": risk.get("code"),
                             "requests_in_window": count,
                             "window_s": window_s,
                             "at": risk.get("iso")})

    duration = 0.0
    if requests:
        duration = float(requests[-1].get("t") or 0) - float(requests[0].get("t") or 0)

    return {
        "events": len(rows),
        "requests": len(requests),
        "duration_s": round(duration, 1),
        "requests_per_min": round(len(requests) / (duration / 60), 2) if duration > 0 else None,
        "codes": dict(codes),
        "rolls": len(rolls),
        "roll_reasons": dict(roll_reasons),
        "roll_failures": sum(1 for r in rolls if not r.get("ok")),
        "throttles": len(throttles),
        "risks": len(risks),
        "risk_codes": dict(risk_codes),
        "rate_at_risk": rate_at_risk,
    }
