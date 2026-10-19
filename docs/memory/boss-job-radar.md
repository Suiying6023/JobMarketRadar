---
name: boss-job-radar
description: JobMarketRadar 通用数据服务：网页与Agent共用任务/配置/日志、画像可选、LLM版本、项目与个人工作区边界
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 通用数据服务（JobMarketRadar）

工程 `D:\Code\Boss`，远端 `Suiying6023/JobMarketRadar`。入口为 `HANDOFF.md`，
操作真源 `docs/OPERATIONS.md`、`docs/SERVICE_API.md` 与 `docs/DATA_MODEL.md`。

## 当前能力

网页和 Agent CLI 管理同一任务核心：采集、详情补全、状态刷新、LLM 打标、进度、停止和实时日志。
启动 `uv run job-radar serve --config config.local.yaml`，默认 8765；Agent 使用
`job-radar task start/list/show/events/stop` 和 `config get/set`。独立 `run` 模式用于单次任务。

城市选项综合内置值、配置及缓存中的平台码。关键词分组搜索、分页、批量编辑；岗位池列表分为信息、
标签、操作三列。LLM 端点、模型、密钥和 prompt 可配置。配置修订检测覆盖多页面和后台轮询，
实际生效与等待新任务的字段分别呈现。

岗位池带**在招状态**筛选与徽标（`open/warm/cold/stale/closed/unknown`），
同优先级内按在招程度排序；判定逻辑只在 `analysis/market.py::hiring_of` 做一次，
前端只显示不重算。判据见 [[boss-refresh-chain]]。

通用学历、届别、工作年限、意向、技能、可达性评分和推荐可保留；默认画像宽泛。
`service.candidate_filtering` 默认关闭，显式开启才用于任务选行。个人研究、简历和面试在 [[job-hunt-workspace]]。

## 运行和存储

每任务独立进程；任务/事件位于 `data/service-state.sqlite3`，手工记录位于 `review-state.sqlite3`。
Windows 先挂起创建进程、登记 Job Object 再执行，覆盖 venv 启动器创建的真实 Python 和浏览器子进程；
超时停止按整棵任务树清理。等待验证时可显示真实窗口；账号与任务恢复规则见 [[boss-risk-control]]。
当前数据与标注版本见 [[boss-data-and-filtering]]，刷新路线见 [[boss-refresh-chain]]。

`local/README.md` 导航本机技术档案，归置与清理回执在 `local/organization/20260913/`。
活动源库与求职分析快照分别维护，备份清理回执为 `backup-removal.json`。
`output/` 的兼容入口属于 JobHunt 历史岗位池，常规导出应显式选择目标路径。

数据库、浏览器目录、密钥、个人配置与 local/ 保留本地。测试使用合成数据、浏览器替身和本地假模型端点。
真实平台验证与在线成功率等待账号恢复后复核，见 [[boss-refresh-resume]]。

相关：[[boss-risk-control]] [[boss-data-and-filtering]] [[boss-keywords-and-tagging]] [[boss-refresh-chain]] [[job-hunt-workspace]]
