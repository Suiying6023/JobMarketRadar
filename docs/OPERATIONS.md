# 操作与配置

## 日常离线工作

`job-radar serve --config config.local.yaml` 提供全国概览、岗位池、关键词、配置和数据说明。
工作台只绑定 `127.0.0.1`，不提供采集、解除暂停或发消息的网页接口。
旧 `scripts/serve_pool.py --port 8000` 现在转到同一工作台；旧的无来源校验写接口已退役。

在网页点击“已投”只是本地标记，不会向招聘者发消息。备注和收藏存于数据库同目录的
`review-state.sqlite3`，不改变导入岗位的正文、旧标签或分层；已有 `chat_actions` 记录仍会读取。

保存配置会验证字段、区间和关键词覆盖，拒绝版本冲突；修改前的版本保存在 `.config-history/`。
网页不能更换数据库/浏览器路径，也不能启用在线访问。切换数据库请在本机编辑配置后重启服务。

## 可调整的参考线

| 字段 | 含义 |
|---|---|
| `budget_mode: reference` | 默认。请求量、绝对页号、每轮关键词数超过参考值时提示，仍执行本次显式计划 |
| `budget_mode: enforce` | 用户主动选择。执行同一组本地上限，达到后停止或截断计划 |
| `max_requests_per_day` | 按业务请求尝试统计的日参考线，跨进程记账 |
| `max_requests_per_run` | 单轮请求尝试参考线 |
| `max_page_number` | 绝对页号参考值，不是已验证的平台安全深度 |
| `max_keywords_per_run` | 每轮关键词数参考值 |
| `pages_per_query` / `pages_per_keyword` | 这一次计划实际翻多少页；后者按词覆盖前者 |
| `request_delay_seconds` / `detail_delay_seconds` | 明确的等待区间，不能据此承诺账号安全 |

旧 `over_quota_action` / `over_quota_ladder` 保留兼容，但是否强制预算由 `budget_mode` 决定。
`code37_limit` 为旧配置兼容字段；采集器已停用自动 token 重算，不能通过调大此值重新启用。

参考线来自本地经验，历史“跑到 N 没出问题”无法推出 N 是安全额度。平台实际风控机制未被验证。
请求账本记录业务尝试，整页脚本/图片等资源不逐个计入，不能把它当成全部 HTTP 请求数。

## 暂停与恢复

```powershell
uv run job-radar risk-status --config config.local.yaml
uv run job-radar risk-pause "人工记录的平台限制" --config config.local.yaml
```

这些命令只操作本地状态。`<数据库目录>/access-policy.json` 缺失、损坏、暂停或
`network_enabled: false` 都会拦住在线入口。暂停不会因跨日、重启或预计时间到期自动解除。
网页保存配置不改变暂停状态。若平台明确要求等待，预计时间只是“最早可复核”时间，不能代替实际确认。

用户人工确认账号恢复后可使用 `risk-release --confirm-account-recovered`；解除仅修改本地状态，
不会自动采集。在线配置仍需显式 `network_enabled: true`。当前账号仍有限制时不执行此操作。

采集入口遇到 HTTP 401/403/429、限制码或验证内容会保存暂停并停止；code=37 只说明需要人工判读，
不将它直接等同封禁或安全，不自动重算 token，也不继续切词尝试。
账本不可用则停止，因为这时无法可靠记录实际请求；它与经验参考线是不同的问题。

## 有界在线入口（待账号恢复后再验证）

- `job-radar collect --config ...`：列表与详情，采用配置候选画像；所有结果仍分层保存。
- `scripts/expand_list.py --config ...`：列表扩展，共用采集器的记账和风险判读；`--restart` 可显式从首页重扫。
- `scripts/backfill_details.py --config ...`：已有记录补正文，先按当前画像复核列表事实。
- `scripts/refresh_status_loop.py`：历史匿名状态刷新链，默认不启用；另需 `--enable-online` 与可用本地策略。
  其 `--budget-mode reference|enforce` 与采集侧同义。历史 warm/rewarm 机制保留待复核，本版未在线验收。

历史夜间调度、出口/账号探测和 `probe_*` 脚本不是工作台自动任务；运行前均接入本地暂停检查。
它们保留研究与兼容用途，不应由保存配置、打开网页或定时器自动触发。`_archive/` 只作历史证据。

## 验证与发布

```powershell
uv run python -m pytest -q
uv build
```

测试使用临时数据库与浏览器替身，不需要 BOSS 账号，不启动真实浏览器。
在线成功率、账号恢复状态与任何“安全吞吐量”均不属于离线测试能证明的结论。
公开仓只保存代码和合成测试数据；数据库、批次 JSON、浏览器 profile、`.env`、个人配置和报告不入库。
