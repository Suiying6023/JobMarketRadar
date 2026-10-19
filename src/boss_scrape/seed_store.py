"""把 stoken 的种子落盘，让冷启动不必再整页加载。

## 省掉的是什么

冷启动必须先拿到一个有效 `__zp_stoken__`。种子是**会话 cookie**（无 Expires），
浏览器一关就没了，所以原先每次启动都要加载一次搜索页让站点 JS 自己算 token。
那一次加载实测是 92~993 个请求（SPA 会自己发约 40 个接口 + 一堆静态资源）。

把种子和算好的 stoken 存下来，下次启动直接恢复 cookie，然后从
`/robots.txt` 这个极小的同源文档发请求 —— 一个文档请求换掉整页加载。

## 有效期是实测的，不是猜的

token 里编了服务端下发的时间戳（`__zp_sts__`），所以它**会过期**。
过期后服务端回 `code=37`，这时只能退回整页 bootstrap。
`DEFAULT_MAX_AGE_S` 是保守值，真实窗口见实验记录（`seed-restore.json`）。

## 存的是凭据，要注意

文件里是 cookie 原值。只放在本机 profile 目录旁边，不进版本库、不进日志。
`RunLog` 里刻意只记长度不记值。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 参与持久化的 cookie。只存这四个——`bst` 之类的另有生命周期，不混进来。
STATE_COOKIES = ("__zp_sseed__", "__zp_sname__", "__zp_sts__", "__zp_stoken__")

# 保守上限。超过它就当没有，直接走整页 bootstrap，不去赌服务端还认不认。
DEFAULT_MAX_AGE_S = 900.0


class SeedStore:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.saved_at: float | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def save(self, jar: dict[str, str]) -> bool:
        if self.path is None:
            return False
        state = {name: jar[name] for name in STATE_COOKIES if jar.get(name)}
        if len(state) != len(STATE_COOKIES):
            # 不完整就别写，免得下次拿半套状态去恢复。
            self.last_error = f"状态不完整: {sorted(state)}"
            return False
        payload = {"saved_at": time.time(), "cookies": state}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)  # 原子替换，别留半个文件
            self.saved_at = payload["saved_at"]
            return True
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            return False

    def load(self, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> dict[str, str] | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            return None
        saved_at = payload.get("saved_at")
        cookies = payload.get("cookies") or {}
        if not isinstance(saved_at, (int, float)) or not isinstance(cookies, dict):
            self.last_error = "状态文件格式不对"
            return None
        age = time.time() - float(saved_at)
        if age > max_age_s:
            self.last_error = f"状态已过期 {age:.0f}s > {max_age_s:.0f}s"
            return None
        if any(not cookies.get(name) for name in STATE_COOKIES):
            self.last_error = "状态缺少必要 cookie"
            return None
        self.saved_at = float(saved_at)
        return {name: str(cookies[name]) for name in STATE_COOKIES}

    def age_seconds(self) -> float | None:
        return None if self.saved_at is None else time.time() - self.saved_at

    def clear(self) -> None:
        if self.path is not None and self.path.exists():
            try:
                self.path.unlink()
            except OSError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
