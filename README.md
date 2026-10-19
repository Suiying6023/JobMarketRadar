# JobMarketRadar

通用岗位数据服务：采集、补全、刷新与打标，使用网页或 Agent CLI 管理同一套任务、配置和日志。

- 一站式任务启停、实时进度与日志、人工验证入口。
- 城市与筛选项下拉选择，关键词分组、检索、分页与批量导入。
- LLM 端点、模型、密钥和提示词配置，标注版本与来源可追溯。
- 刷新默认本地直连，可配置 HTTP/SOCKS 代理。
- 岗位池总览与可配置画像复核；个人求职材料使用独立工作区。
- 经验参数默认作为参考；在线任务按访问配置与持久状态执行。
- 按批次记录本轮岗位、续跑未完成查询，并导出独立分析快照。

Python 3.11+，使用 uv：

```powershell
uv sync --extra dev
uv run job-radar demo --directory demo
uv run job-radar serve --config demo/config.local.yaml
```

打开 `http://127.0.0.1:8765/`。Agent 使用相同服务：

```powershell
uv run job-radar task start collect --dry-run
uv run job-radar task list
uv run job-radar config get
```

[四条链路](docs/PIPELINE.md) · [操作与配置](docs/OPERATIONS.md) · [Agent/API](docs/SERVICE_API.md) · [数据模型](docs/DATA_MODEL.md) · [工作区边界](docs/WORKSPACES.md) · [有界维护](docs/NIGHT_MAINTENANCE.md)
