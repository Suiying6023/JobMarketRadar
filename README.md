# JobMarketRadar

本地招聘数据工作台：先分析全国采样池，再用可调整的求职画像生成岗位复核队列。

- 全国整库的城市、行业、薪资、经验与技能统计，明确显示分母和采样偏差。
- 岗位搜索、原文阅读、可解释的复核排序，以及单独保存的收藏、已投和备注。
- 关键词覆盖、独有覆盖、翻页配置与候选画像编辑；配置校验、冲突检测和版本备份。
- 经验参考线默认只提示，可显式选择强制执行；两种方式都不代表平台安全额度。
- 持久暂停、请求记账和故障停止；默认离线，保存配置不会启动浏览器或采集。
- 原 SQLite 只读分析；原始数据、账号状态、个人配置和浏览器会话留在本机。

## 运行

Python 3.11+。使用 uv：

```powershell
uv sync --extra dev

# 用合成数据体验（不会联网，不含真实岗位）
uv run job-radar demo --directory demo
uv run job-radar serve --config demo/config.local.yaml
```

打开 `http://127.0.0.1:8765/`。已有数据库时：

```powershell
uv run job-radar init --config config.local.yaml --database data/jobs.sqlite3
uv run job-radar serve --config config.local.yaml
uv run job-radar analyze --config config.local.yaml --output output/market-analysis.json
uv run job-radar db-backup --config config.local.yaml --destination data/backups/jobs-copy.sqlite3
```

`init` 仅创建配置，不抓取数据；数据库应为本项目的 SQLite schema。相对数据库路径在工作台中按配置文件所在目录解析。

[操作与配置](docs/OPERATIONS.md) · [数据口径](docs/DATA_MODEL.md) · [脚本索引](scripts/README.md)
