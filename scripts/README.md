# 工具索引

## 当前推荐入口

| 任务 | 入口 |
|---|---|
| 网页配置、关键词、任务与日志 | `job-radar serve --config config.local.yaml` |
| Agent 管理任务 | `job-radar task start/list/show/events/stop` |
| 独立运行同一任务核心 | `job-radar run collect/details/refresh/tag` |
| 通用岗位池统计导出 | `job-radar analyze --config config.local.yaml` |
| 一致性备份 | `job-radar db-backup --destination ...` |
| 本地暂停状态 | `job-radar risk-status` / `risk-pause` |
| 无账号合成演示 | `job-radar demo --directory demo` |
| 旧页面启动命令 | `serve_pool.py`（已转到新工作台） |

## 采集与离线处理脚本

以下保留历史兼容与研究用途。新的运行流程使用 [服务入口](../docs/SERVICE_API.md)，共享任务、配置、日志与取消机制。

- `expand_list.py`：列表；`backfill_details.py`：补详情；`refresh_status_loop.py`：历史状态刷新。
  默认离线。实际平台限制单独暂停；自设参考线默认提示，强制限制需显式选择。详见 [操作说明](../docs/OPERATIONS.md)。
- `backfill_from_raw.py`：从缓存补字段；`clean_local_db.py`、`apply_glm_filters.py`：旧分层规则。
  后两者会改原库，运行前备份；新工作台复核不依赖它们的结论。
- `extract_glm_tags.py`、`jd_semantic_survey.py`：外部模型抽取工具，会将选中的 JD 发给配置的模型服务；
  不属于离线工作台的自动动作，模型标签始终需要抽查。
- `analyze_*.py`、`market_signals.py`、`capability_coverage.py`：历史专题统计；先核对分母与词表再引用数字。
- `export_pool.py` / `export_pool_html.py`：旧个人投递池导出；新工作台提供完整池与可调画像。

## 历史编排与实验

`daily_cycle.py`、`overnight_supervisor.py`、`sustained_run.py` 为旧编排链。
`probe_*`、`calibrate_*`、出口/登录/浏览器辅助脚本保留本地实验用途；已接入本地暂停检查，
`--help` 不会启动浏览器。`_archive/` 只作历史证据，不在推荐工作流中。

历史文档里“安全线”“原因已确认”等说法是当时的实验解释，不能作为平台机制或额度承诺。
当前数据与配置口径见 [数据说明](../docs/DATA_MODEL.md)。
