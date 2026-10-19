"""`decision-model-preview` 的客户端（TypeSafe System One 协议）。

## 协议要点（踩过坑，别用 chat 载荷）

    POST {base}/systemone
    {
      "model": "decision-model-preview",
      "state": <字符串 / 对象 / 数组>,
      "questions": {
        "<问题id>": {"type": "choice", "instructions": "...", "criteria": {选项: 描述}},
        "<问题id>": {"type": "noul",   "instructions": "..."},
        "<问题id>": {"type": "score",  "instructions": "...", "criteria": ["低", ..., "高"]}
      }
    }

返回 `answers.<id>`，按类型给 `choice` / `noul` / `score`，外加
`probabilities` 与（choice/score 的）`confidence`。**不生成文本。**

用 `/chat/completions` 打这个模型一律 400 `invalid_request`——
我最初就是这么试的，改了九种载荷组合全是 400，因为路径和结构都不对。

## 并发

决策模型不生成文本，服务端耗时与输出长度无关，所以并发收益是线性的。
实测单条 2.3 秒；12–16 并发足够把 7,417 条压到一小时出头。
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

SYSTEMONE_PATH = "/systemone"
MODEL = "decision-model-preview"
# 4xx 里这几个值得重试：429 限流、408 超时、5xx 是上游抖动。
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class DecisionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class DecisionClient:
    base_url: str
    api_key: str
    model: str = MODEL
    timeout: float = 90.0
    max_retries: int = 3
    # ⚠️ `urllib` 的 OpenerDirector **不是线程安全的**：它内部有 handler 链表和
    # 共享状态，多线程并发 open() 会互相踩。第一版所有线程共用一个 opener，
    # 结果 14 并发下 45 秒只完成 1 条、其余全部挂住（不是报错，是卡死，最难查）。
    # 改成每线程一个 opener。
    opener: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.calls = 0
        self.failures = 0
        self._local = threading.local()
        if self.opener is not None:
            self._local.opener = self.opener

    @property
    def _opener(self):
        opener = getattr(self._local, "opener", None)
        if opener is None:
            opener = build_opener(ProxyHandler({}))
            self._local.opener = opener
        return opener

    def decide(self, state, questions: dict) -> dict:
        """一次决策。返回 `answers` 字典；失败抛 DecisionError。"""
        payload = {"model": self.model, "state": state, "questions": questions}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: DecisionError | None = None

        for attempt in range(self.max_retries + 1):
            request = Request(
                f"{self.base_url}{SYSTEMONE_PATH}", body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "Authorization": "Bearer " + self.api_key})
            self.calls += 1
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(500_000)
                data = json.loads(raw)
                answers = data.get("answers")
                if not isinstance(answers, dict) or not answers:
                    raise DecisionError("响应缺少 answers", retryable=True)
                return answers
            except HTTPError as exc:
                detail = exc.read(600).decode("utf-8", "replace") if exc.fp else ""
                retryable = exc.code in RETRY_STATUS
                last_error = DecisionError(f"HTTP {exc.code}: {detail[:200]}",
                                           status=exc.code, retryable=retryable)
                if not retryable:
                    self.failures += 1
                    raise last_error from None
            except (URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = DecisionError(
                    f"连接失败: {type(reason).__name__}: {reason}", retryable=True)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = DecisionError(f"响应解析失败: {exc}", retryable=True)

            if attempt < self.max_retries:
                # 指数退避 + 抖动：并发下同时重试会把上游再打一遍。
                await_time = min(30.0, 1.5 * (2 ** attempt)) * random.uniform(0.8, 1.2)
                time.sleep(await_time)

        self.failures += 1
        assert last_error is not None
        raise last_error


def run_batch(client: DecisionClient, items: Iterable[dict], *,
              workers: int = 12, state_of=None,
              questions: dict | None = None) -> Iterator[tuple[dict, dict | None, str | None]]:
    """并发跑一批。逐条产出 `(item, answers, error)`，**不缓存、不落盘**。

    调用方负责断点续跑（跳过已完成的 id）。这里只保证：
      · 单条失败不影响整批
      · 异常不吞，作为 `error` 字符串交出去
    """
    state_of = state_of or (lambda item: item)
    from .schema import QUESTIONS
    questions = questions or QUESTIONS

    def one(item: dict):
        return client.decide(state_of(item), questions)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                yield item, future.result(), None
            except DecisionError as exc:
                yield item, None, str(exc)[:300]
            except Exception as exc:  # noqa: BLE001 - 单条异常不能拖垮整批
                yield item, None, f"{type(exc).__name__}: {exc}"[:300]
