# 风控与服务设计参考

核实日期：2026-09-13。下列是架构和维护线索，节奏数值按具体运行条件验证。本项目的实现使用自身代码与本地替身测试。

| 来源 | 可参考的设计 |
|---|---|
| [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) | 结构化错误、凭据状态、请求间隔与并发锁 |
| [get_jobs](https://github.com/loks666/get_jobs) | 任务启停、事件流、断线重连与人工验证等待 |
| [boss-helper](https://github.com/Ocyss/boss-helper) | 配置分组、请求超时、状态与操作反馈 |
| [boss-cli](https://github.com/jackwener/boss-cli) | 对认证、平台限制及网络失败分别报告 |
| [auto-zhipin](https://github.com/ufownl/auto-zhipin) | 历史浏览器采集流程参考 |

重点代码：[限流器](https://github.com/can4hou6joeng4/boss-agent-cli/blob/master/src/boss_agent_cli/api/throttle.py)、[事件流重连](https://github.com/loks666/get_jobs/blob/main/front/lib/sse.ts)。项目许可和维护状态按采用时版本核查；本轮没有复制这些项目的实现。

本版落实：任务和日志持久化、同数据池的活动资源串行、请求间隔与批间等待、平台验证暂停、按任务归属恢复、明确的直连/代理配置、密钥脱敏与合成验证。

账号恢复后的验证顺序：

1. 人工确认真实平台状态，再选择一个明确的城市/关键词或少量已有岗位。
2. 分别验证列表、详情、状态页面；记录目标、出口、会话、时间和原始结果。
3. 触发人工验证时检查暂停来源、窗口入口、恢复后的业务响应及资源清理。
4. 对照岗位原文核对关闭/开放/未知状态与标签质量，再决定是否扩量。

旧经验用于设定可调参考线。代理承担网络连接职责；平台限制、待验证状态和实际业务结果单独记录。
