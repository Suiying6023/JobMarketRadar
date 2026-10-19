# 操作与配置

## 网页与任务

`job-radar serve --config config.local.yaml` 在本机启动管理服务。页面提供岗位池总览、岗位阅读、关键词、配置和任务日志。每个数据池由一个管理服务负责；CLI 和网页共用它的任务接口。

| 任务 | 行为 |
|---|---|
| collect | 按城市、关键词和筛选组合采集，按配置补全详情 |
| recommend | 读取当前求职期望下的原生推荐分页，独立保存批次与上下文哈希 |
| details | 选择缺少或过期 JD 的记录，使用保存的列表信息更新正文 |
| refresh | 检查岗位页面状态，保留检查证据与独立时间 |
| tag | 用配置的 LLM 提取岗位信息，保存输入/提示词/模型版本 |

任务的数量上限控制本次处理规模。「计划预览」生成计划，适合检查配置；执行模式在任务页显式选择。
默认选行面向数据池，开启 `service.candidate_filtering` 才应用候选人事实条件。城市、学历、届别、年限、意向和技能均来自配置。

## 登录、新一轮采集与续跑

```powershell
uv run job-radar login --config config.local.yaml
```

此命令打开可见浏览器，供本人登录和核对账号状态。关闭窗口或按 Ctrl+C 保存会话；它不自动解除本地暂停或启动采集。确认搜索和详情正常后，在访问控制里恢复访问并开启联网配置。

使用一个批次编号组织同一轮数据。采集任务的数量上限按该批次的新岗位主键计数，多关键词重复命中不会占用上限。

```powershell
uv run job-radar task start collect --round-id 20260930-ai --limit 500 --dry-run
uv run job-radar task start collect --round-id 20260930-ai --limit 500
uv run job-radar task start details --round-id 20260930-ai --limit 100
uv run job-radar rounds --config config.local.yaml
uv run job-radar export-round 20260930-ai D:\Code\JobHunt\data\snapshots\boss-20260930.sqlite3
```

采集未指定编号时使用任务 ID 创建新批次。相同编号跳过已完成的城市/关键词/筛选查询；在查询中途停止时，下次从这组查询第一页重跑，已入批次的岗位不重复占用上限。城市、关键词、筛选、页深或选行规则改变时应使用新编号；已经完成的批次不会自动重采。

补详情、刷新和打标指定批次时只选择这轮搜到的岗位，不指定时选择全库。`service.detail_after_hours` 默认 168 小时，0 表示再次更新所有非未来时间的正文。采集与补详情共享这个时效；列表结果先落库，详情失败仍保留观察记录。新正文按获取时间替换，缩短也可更新；补详情不改变列表观察时间与次数。

批次快照使用这一轮保存的记录导出，后续全库变化不改写它。JSON 清单记录查询计划、完整/部分状态、岗位数和 SHA-256。正文可能沿用时效期内的旧文本，判断新正文时查看 `detail_fetched_at`。旧岗位本轮未出现只表示未被这次查询搜到。

手工收藏、已投和备注保存在 `review-state.sqlite3`。任务状态及日志在同目录 `service-state.sqlite3`；岗位数据由原数据库保存。

## 配置生效

保存使用修订号检查并发修改。空密钥输入保留已有值，清除使用专门勾选；HTTP 响应和历史配置隐藏密钥。

| 配置 | 生效时间 |
|---|---|
| 在线访问开关 | 下一次平台请求前 |
| LLM、批大小、请求节奏与预算 | 下一批次 |
| 浏览器、代理、刷新路线、搜索计划与候选选行 | 新任务 |
| 数据库位置 | 重启服务 |

采集以查询组合为批次，其他任务按服务批大小处理。任务开始时确定目标集合，新的 prompt 应用于剩余批次；重新标注已完成或先前已跳过的岗位时，新建打标任务。
仅部分配置生效时，任务记录有效配置摘要，并列出等待新任务的字段。运行中的连接与浏览器保持其已选择的出口。

城市下拉综合内置表、已存配置和岗位缓存中的真实平台码；未知城市可补充名称与码。多组筛选条件依次执行，单维度支持逗号分隔多选。

## 在线访问与人工验证

采集、详情和刷新同时检查 `network_enabled` 与数据库目录的 `access-policy.json`。访问关闭或暂停时可继续查看本地数据、编辑配置及预览任务。LLM 打标独立使用 `llm.enabled` 和模型配置。

浏览器岗位页持续空白、跳到其他来源或关闭时，采集/详情任务以 `BrowserPageUnavailable` 停止，不反复导航搜索页，也不将它记为平台账号风险。先确认普通浏览器的搜索和详情正常，再检查工具兼容性。HTTP 200 或工具能操作本机控制台，都不足以确认官网岗位采集可用。

任务仍有失败记录时最终状态为失败，成功写入的数据保留在进度和结果中。模型对已识别的瞬时连接异常最多重试一次；认证、HTTP错误和输出校验失败不自动重试。模型用量记录 `client_transport_attempts`，它是客户端尝试次数，不表示丢失响应的请求一定未计费。

平台验证触发 `waiting_verification`，页面可以显示任务浏览器窗口。本机采集/详情的暂停保存在 access-policy.json；匿名刷新自己的暂停保存在 refresh-access-policy.json，互不替代。全局人工暂停仍覆盖所有平台任务。用户完成验证后点击继续，系统复检实际业务响应。任务只能解除自身触发的暂停；独立人工暂停由访问控制面板确认恢复。
全局暂停覆盖所有活动平台任务。停止先执行协作取消，超时后清理本任务进程树；服务重启将遗留活动状态标为 interrupted，任务由操作者重新发起。

```powershell
uv run job-radar risk-status --config config.local.yaml
uv run job-radar risk-pause "人工暂停原因" --config config.local.yaml
```

人工确认平台可用后使用页面访问控制，或 `risk-release --confirm-account-recovered`。时间预测用于复核安排，恢复操作以实际平台状态为依据。

## 路线与参考参数

刷新默认本地直连，使用独立浏览器目录 `.browser-refresh`。选择代理时填写 `refresh.proxy_server` 及可选认证字段。
原国内跳板路线通过 SSH SOCKS 接入：在本机启动到所选服务器的 SSH 动态转发，监听回环地址，再将刷新设为 proxy 并填写对应的 `socks5://127.0.0.1:<端口>`。端口监听与首页响应只检查连接，岗位刷新必须核对实际岗位标题、状态及活跃度。跳板只转发流量，不需要部署浏览器或采集代码。
跳板刷新使用独立匿名会话，拒绝复用采集登录目录或带登录凭据的浏览器。平台需要人工验证时只完成验证；账号登录继续在本地采集会话进行。刷新同时记录状态和可读取的招聘者活跃度，保持正文获取时间不变。
采集和详情补全使用 `browser.proxy_server/proxy_username/proxy_password`；CDP 模式继承所连接浏览器的网络配置。
地址支持 HTTP/HTTPS/SOCKS5，`socks5h://` 在 Chromium 启动时统一使用 `socks5://` 协议名。会话目录应放在 Git 忽略区域。
请求延迟、突发等待、详情批间等待由共用调度器执行。等待可被任务停止打断；业务请求按请求 ID 记录并回填结果。

请求量只做**观测**（`request_log`），不再有日线拦截：`budget_mode` / `max_requests_per_day` / `max_requests_per_run` 已于 2026-10-19 移除。理由是按历史轮次反推的日线不是平台额度，把它当硬线等于在证据不足时替用户做决定。节奏由 `request_delay_seconds` / `detail_delay_seconds` / `detail_batch` / `burst_threshold` 控制；出事即停由 `access-policy.json` 的持久熔断负责。这些数值是操作参数，平台行为通过实际响应判读。
HTTP 限制或验证码进入人工处理；未知状态保留证据，连续无法判读时停止当前轮次。状态刷新使用成功检查时间决定下次是否需要刷新，独立于 JD 正文时间。

## LLM

支持 Chat Completions 兼容端点。填写 API 根地址、模型和提示词；本机密钥优先，`api_key_env` 为环境变量回退。运行打标时，所选岗位字段会发送到该端点。
`llm.concurrency` 控制同时抽取的数量，实际单批取它与 service.batch_size 的较小值，可在下一批调整。请求超时、输出上限和推理强度可配置；当前 space-bunny 的 auto 使用已实测的 low，不发送 JSON mode。
任务保留提示词和完整来源文本摘要、模型、端点及输出。原文或标注配置变化后，旧标注显示为待更新。模型响应须为非空 JSON 对象。默认提示词要求的字段及引文位置经过校验，输出截断不落入成功标注。一次批次有至少3个失败，或连续失败累计3次时停止，已经保存的结果保留。抽取只输入岗位数据，不注入候选人的目标技能。

## 开发与发布

```powershell
uv run python -X utf8 -m pytest -q
node --test tests/web_config_state.test.mjs
uv build
```

Node 22+ 用于前端状态测试，网页本身无需构建。测试使用合成数据库、浏览器替身和本地假模型端点。实际平台连通与验证效果在账号恢复后做小样本验收。
私人数据、浏览器会话、本地配置与 `local/` 保留本机。
（`scripts/` 与 `local/archive/` 已在 2026-10-19 的项目瘦身中删除；一次性研究脚本
现在放 `local/analysis/<日期>/`，不进仓库。）

有截止时间的周期维护使用 `job-radar maintenance --plan <计划JSON>`，调度由Mirasim管理，
`--inspect-only`检查现场。维护共用任务锁与暂停机制，验证/访问限制后停止，
不自动恢复；到期停止本计划任务，并按配置修订号决定是否恢复基线。示例与参数见
[本夜维护说明](NIGHT_MAINTENANCE.md)。

读取独立刷新暂停可用 `job-radar risk-status --scope refresh`；pause/release 同样支持 scope。恢复仍需实际核验和明确操作。

## 正文与登录核验

浏览器内残留 wt2/bst 等 Cookie 不证明服务端仍登录。详情前检查可见登录入口；匿名预览可能是旧正文的一半或最多400字。可判定的预览单独保存 detail_preview，并保留旧完整正文及其原时间。已验证访问的详情写 raw_json.detail_access_verified=true；false 标记会重新获取，按批次打标只消费已验证的正文。

详情按 detail_batch 正常加载搜索上下文。遇到 code=37 仍进入人工复检，不自动解除暂停。代理刷新使用独立匿名会话；同一账号在另一个窗口的登录/退出可能改变本机实际登录，必须核对完整正文。
