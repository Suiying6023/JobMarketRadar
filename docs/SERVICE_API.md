# Agent 与 HTTP API

## 服务模式

先运行 `job-radar serve --config config.local.yaml`，然后通过网页、CLI 或 HTTP 管理同一个数据池。

```powershell
uv run job-radar task start collect --dry-run --limit 100
uv run job-radar task list
uv run job-radar task show TASK_ID
uv run job-radar task events TASK_ID --after 0
uv run job-radar task stop TASK_ID
uv run job-radar config get
uv run job-radar config set settings.json
```

命令结果为 UTF-8 JSON。`config set` 文件包含 `{config, revision, clear_secrets?}`，修订号来自 `config get`，用于检测并发编辑。指定其他本机端口时使用 `--url http://127.0.0.1:PORT`。

验证控制：`task verify-window TASK_ID` 显示真实任务窗口；用户完成验证后 `task resume TASK_ID --confirmed` 复检当前请求。

## 独立模式

```powershell
uv run job-radar run collect --config config.local.yaml --dry-run
uv run job-radar run tag --config config.local.yaml --limit 100
```

独立模式拥有一个任务管理器，结束后输出最终任务 JSON。需要持续人工验证时使用服务模式。`collect` 是 `run collect` 的兼容入口。
同一数据池同时只运行一个管理器；代码调用可使用 `TaskManager(SettingsStore(path))`。数据任务使用自身的进程及浏览器资源。

## HTTP

所有 POST 使用 JSON 和 `X-CSRF-Token`，令牌由 `/api/state` 提供。服务绑定回环地址并校验 Host/Origin。

| 请求 | 用途 |
|---|---|
| GET /api/state | 脱敏配置、修订号、访问状态、生效时点 |
| POST /api/config | 保存配置；空密钥保留已有值，clear_secrets 显式清除 |
| GET /api/options | 城市/平台筛选值和任务类型 |
| GET /api/tasks | 活动任务优先的任务列表，及全部活动数量 |
| POST /api/tasks | `{kind, options:{limit,dry_run}}`，返回 202 + Task |
| GET /api/tasks/{id} | 任务状态、进度、配置版本、结果、验证信息 |
| GET /api/tasks/{id}/events?after=0&limit=200 | 递增游标日志，返回 items/next_cursor |
| POST /api/tasks/{id}/stop | 请求停止并清理任务资源 |
| POST /api/tasks/{id}/verification | 显示任务验证窗口 |
| POST /api/tasks/{id}/resume | `{confirmed:true}`，仅处理该任务的验证暂停 |
| POST /api/access | `{action:'pause',reason}` 或 `{action:'release',confirmed:true,confirmation}` |
| GET /api/overview | 岗位池总体统计 |
| GET /api/jobs、/api/jobs/{id} | 岗位检索、原文、通用复核与最近标注 |
| GET /api/keywords | 配置词表及本地覆盖统计 |
| GET /api/export/jobs.csv | 按当前检索条件导出 CSV |
| POST /api/jobs/{id}/action | `{action, value}`，保存本地关注、已投或备注 |

岗位列表与 CSV 支持 `q`、`city`、`tier`；列表另支持 `page`、`page_size`（1–100）。
本地记录的 action 为 `bookmark` / `applied` 时 value 使用布尔值，`note` 使用最多 3000 字的字符串，`reset` 清空该岗位的本地记录。该 POST 同样需要 CSRF 令牌。

状态：queued → running → succeeded/failed/cancelled；运行中可进入 waiting_verification 或 stopping。重启后的遗留活动任务为 interrupted。终态保持不变。
进度里的 `pending_configuration` 列出等待新任务生效的字段；`applied_revision` 以 `partial:` 开头时表示配置部分生效。标注的 `stale` 表示原文或配置与标注来源版本不同。

参数错误返回 400/422，任务或修订冲突为 409，不存在的任务为 404。错误 JSON 使用 `error` 字段。日志保留条数受 service.event_retention 配置控制。
