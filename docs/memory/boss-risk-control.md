---
name: boss-risk-control
description: BOSS 访问与任务恢复：显式在线开关、持久暂停、任务归属校验、真实平台风险与本地参考线分开
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
