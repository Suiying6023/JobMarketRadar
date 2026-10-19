---
name: boss-risk-control
description: BOSS 访问与恢复：读页面原文精确判风控类别、三类停机（滑块/退出/IP限）分开提示、持久暂停与人工解除
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 访问状态与经验参数

实现真源为 `D:\Code\Boss\src\job_radar\safety.py`、`tasks.py`、`worker.py`、`collectors/boss.py`。
现场账号状态见 [[boss-refresh-resume]]。

## 访问与恢复

平台任务同时检查 network_enabled 和数据库目录的 access-policy.json。每次请求前检查访问状态；
修改配置不会自动解除持久暂停，预计日期也不会自动放行。平台返回的风险与本地预算停止采用不同异常类别。

平台验证在任务和访问状态里标记 `managed-task:<id>`，页面提供真实窗口与人工复检入口。
任务继续只能解除同源任务暂停，独立人工暂停由访问控制单独确认；来源校验和解除在同一文件锁内执行。
全局暂停查询全部活动任务，任务列表优先显示活动项。停止先协作取消，超时清理本任务进程树。

风险事件先写任务日志；暂停状态写入失败时结束任务并保留失败原因。等待验证期间仍响应停止。
账户实际是否恢复依据平台状态，复检使用当前目标的真实业务响应。

## 停机时怎么读平台的话

平台不总是回 `code`。很多时候它直接把说明写在页面上：

```
您的IP地址存在异常行为
您的账号可能存在异常访问行为，完成验证后即可正常使用
访问过于频繁，请稍后再试
```

`_page_prompt()` 剥掉 script/style 取可见文本、挑含风控词的句子；
`_prompt_verdict()` 把原话翻成 `(category, code)`：

| 页面文案 | category | code |
|---|---|---|
| IP地址存在异常 | `network_restriction` | 35 |
| 账户存在异常 | `account_restriction` | 36 |
| 环境存在异常 | `environment_restriction` | 37 |
| 访问过于频繁 | `rate_limited` | — |
| 请先登录 / 未登录 | `authentication_required` | 7 |

**文案优先于 URL 模式和字节数区间**。实测一个 1KB 的「IP地址存在异常」页，
按字节数会归成 `empty_page`（当成我们自己的 bug）—— 短页在字节分档里毫无分辨力。

⚠️ `_page_prompt()` **没命中风控词时返回空串**：正常岗位页的首句是 JD 正文，
把它当成「平台提示」写进异常消息只会误导。

## 三类停机要人做的事完全不同

`worker.py` 的弹窗文案按 category 分开：

- `verification_required` → 请直接拖滑块
- `authentication_required` → **账号已退出**，请重新登录（不是滑滑块）
- `network_restriction` (35) → **出口 IP 被限制**，账号本身没问题，换出口或等冷却

2026-10-19 之前一律提示「请人工确认账号状态」，把三件事糊成一句 —— 人打开窗口
看到的是岗位页，不知道该拖滑块、该登录、还是该换出口。

## 节奏与观测（没有请求预算）

`request_log` 只做观测：记时间、种类、结果，**不拦截任何请求**。`budget_mode` /
`max_requests_per_day` / `max_requests_per_run` / `over_quota_*` 已整体移除——
日线是按历史轮次反推的推测值，不是平台额度，把它当硬线等于在证据不足时替用户做决定。

节奏由四组参数控制：`request_delay_seconds`（列表）、`detail_delay_seconds`（详情）、
`detail_batch` + `detail_cooldown_seconds`（批间冷却）、`burst_threshold` +
`burst_penalty_seconds`（连续请求后的长停顿）。`code37_limit` 保留——它是重算次数熔断，
属于风控不属于预算。

外部项目参考及后续验证方法在工程 docs/SERVICE_REFERENCES.md。代理是显式网络出口，
平台验证和访问限制仍走同一持久状态处理。

平台侧的请求墙、风控阶梯与 stoken 机制见 [[boss-scrape-layer]]；这里是本地的访问控制与恢复语义，
两者不是一回事——本地预算停止不能当成平台风控，平台风控也不能靠本地重试解决。

相关：[[boss-job-radar]] [[boss-refresh-chain]] [[boss-refresh-resume]] [[boss-scrape-layer]] [[diagnostic-evidence]]
