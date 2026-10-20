"""把异常页面丢给决策模型判别，而不是靠写死的规则。

## 为什么

2026-10-19 到 20 的事故很说明问题：平台返回的是
`403.html?code=32`「您的账户存在异常行为，已暂时被限制访问！将于 2026-10-21
00:01 恢复正常」，而抓取链路把它判成了 `js_challenge`（「读早了，等 2 秒会自愈」），
于是对一个**已封禁的账号**又发了十几个请求。

事后看，规则版的每一层都能被绕过：

| 层 | 怎么失效的 |
|---|---|
| URL 模式 | `_is_challenge_url()` 只认 security/verify/safe-validate，不认 403.html |
| 字节数区间 | 封禁页 ~44KB，正落在 JS 挑战页的 25–62KB 里 |
| 文案关键词 | 写成 `ip地址存在异常`，平台写的是「您的 **IP** 存在异常行为」 |
| code 编号 | 页面内联了 31 和 32 两个块，只看文案会挑中第一个命中的 |

**根因不是某条规则写错，而是「异常页面的语义要靠人去猜」这件事本身不可靠。**
平台的文案、编号、页面结构随时会变，而每变一次我们就要补一条规则 ——
这和 JD 判定那边已经放弃的做法是同一个错误（见 `analysis/decisions.py`）。

所以：**判断交给模型，机械提取留给规则**。模型回答「这是什么页」，
日期时间这种固定格式的东西仍然用正则抠。

## 用法

```python
from job_analysis.page_state import classify_page, build_page_state
verdict = classify_page(client, text=visible_text, url=page.url, size=n)
verdict["state"]        # 'account_restricted' 等
verdict["action"]       # 'stop' / 'skip' / 'continue'
```

`classify_page` 失败（网络、额度、响应格式）时抛 `DecisionError`；
调用方应当**退回到规则判据**，而不是当作「一切正常」继续跑。
"""
from __future__ import annotations

from .decision import DecisionClient

#: 模型能给出的页面状态。选项刻意覆盖「不是风控」的那几种 ——
#: 否则模型会把一个正常下线的岗位硬塞进风控类别里。
PAGE_STATE_OPTIONS: dict[str, str] = {
    "normal_content": "这就是正常的岗位详情页，只是我们没取到正文容器",
    "job_closed": "岗位已关闭 / 已下线 / 已停止招聘",
    "job_not_found": "岗位不存在或链接失效（404 之类的空页面）",
    "login_required": "要求登录后才能查看，当前未登录或登录已失效",
    "verification_required": "要求人工完成滑块 / 安全验证后才能继续",
    "rate_limited": "提示访问过于频繁 / 操作太快，要求稍后再试",
    "account_restricted": "账号本身被限制访问（不是 IP、也不是要滑块）",
    "ip_restricted": "当前 IP 被限制访问，账号本身没问题",
    "environment_flagged": "提示环境 / 设备存在异常，需要人工判断",
    "page_changed": "页面结构变了，出现了我们不认识的版式",
    "unknown": "以上都不是，或者信息不足以判断",
}

#: 状态 → 采集器该做什么。
#: `stop` = 立刻停机等人，`skip` = 这条跳过继续下一条，`retry_later` = 可以稍后重试。
PAGE_STATE_ACTION = {
    "normal_content": "continue",
    "job_closed": "skip",
    "job_not_found": "skip",
    "login_required": "stop",
    "verification_required": "stop",
    "rate_limited": "stop",
    "account_restricted": "stop",
    "ip_restricted": "stop",
    "environment_flagged": "stop",
    "page_changed": "stop",
    "unknown": "stop",
}

#: 状态 → 采集器内部的 category（与 `boss_payload.response_condition` 同一套词汇）。
PAGE_STATE_CATEGORY = {
    "normal_content": "",
    "job_closed": "",
    "job_not_found": "",
    "login_required": "authentication_required",
    "verification_required": "verification_required",
    "rate_limited": "rate_limited",
    "account_restricted": "account_restriction",
    "ip_restricted": "network_restriction",
    "environment_flagged": "environment_restriction",
    "page_changed": "page_changed",
    "unknown": "unknown",
}

PAGE_QUESTIONS: dict[str, dict] = {
    "page_state": {
        "type": "choice",
        "instructions": (
            "下面是一个招聘网站的页面可见文字（可能还带 URL 和字节数）。"
            "请判断**这个页面在说什么**。\n"
            "1. 如果文字里有明确的岗位职责、任职要求、薪资描述，选 normal_content"
            "（即使正文不全也算）。\n"
            "2. 如果写「已关闭」「已下线」「停止招聘」，选 job_closed。\n"
            "3. 如果整页几乎为空、只有页面框架，选 job_not_found。\n"
            "4. 如果要求登录 / 提示未登录 / 登录已失效，选 login_required。\n"
            "5. 如果要求拖动滑块 / 完成安全验证，选 verification_required。\n"
            "6. 如果提示访问频繁 / 操作过快 / 稍后再试，选 rate_limited。\n"
            "7. 如果提示**账号或账户**存在异常行为、被限制访问，选 account_restricted。"
            "注意：账号限制和 IP 限制是两件事，看文字里说的是「账号/账户」还是「IP/网络」。\n"
            "8. 如果提示当前 **IP** 存在异常行为，选 ip_restricted。\n"
            "9. 如果提示环境 / 设备 / 浏览器存在异常，选 environment_flagged。\n"
            "10. 如果页面是排版正常的一页、但既不像岗位页也不像上面任何提示，"
            "或者文字太少无法判断，选 page_changed / unknown。\n"
            "**一页里出现多个提示时，以「当前真正阻止访问的那一条」为准** ——"
            "通常是最显眼的主标题，而不是被折叠/隐藏的其它提示。"),
        "criteria": PAGE_STATE_OPTIONS,
    },
}

#: 页面文字截断长度。异常页通常很短（<2k），给 6000 足够覆盖任何提示文案。
MAX_PAGE_CHARS = 6000


def build_page_state(text: str, *, url: str = "", size: int = 0,
                     http_status: int | None = None) -> dict:
    """把页面信息转成模型要看的 `state`。

    带上 URL 和字节数：它们是**旁证**，不能单独作判据，但能帮模型在
    「页面几乎是空的」和「页面正常但没容器」之间分开。
    """
    body = (text or "").strip()
    return {
        "url": (url or "")[:300],
        "byte_size": int(size or 0),
        "http_status": http_status,
        "visible_text": body[:MAX_PAGE_CHARS],
        "text_truncated": len(body) > MAX_PAGE_CHARS,
        "text_length": len(body),
    }


def classify_page(client: DecisionClient, text: str, *, url: str = "", size: int = 0,
                  http_status: int | None = None) -> dict:
    """让模型判断异常页面属于哪种状态。

    返回 `{state, confidence, probabilities, action, category}`。
    模型调用失败会抛 `DecisionError` —— **调用方必须退回规则判据**，
    不能把「模型没答上来」当成「页面没问题」。
    """
    state = build_page_state(text, url=url, size=size, http_status=http_status)
    answers = client.decide(state, PAGE_QUESTIONS)
    answer = answers.get("page_state") or {}
    choice = (answer.get("choice") or "").strip()
    if choice not in PAGE_STATE_OPTIONS:
        # 模型回了个不在选项里的值：当成 unknown，让采集器停下来等人看。
        # 不要静默映射到 normal_content —— 那是把「不知道」说成「没事」。
        choice = "unknown"
    return {
        "state": choice,
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities") or {},
        "action": PAGE_STATE_ACTION.get(choice, "stop"),
        "category": PAGE_STATE_CATEGORY.get(choice, "unknown"),
    }
