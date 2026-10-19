# 已取得的结构化数据输入

在线采集通道与岗位解析分开。这个入口只读取文件，将允许取得的结构化响应送入现有解析、去重、时间记录及批次导出。它不连接浏览器、不发平台请求、不执行响应中的脚本，也不处理校验票据。

## 输入格式

- HAR：只处理BOSS列表和详情响应的正文。请求头、响应头、Cookie、认证状态和其他业务接口不进入输出。JSON、表单POST与URL中的查询条件只保留城市、关键词和岗位筛选字段。
- 原生列表JSON：需为 `code=0`、`zpData.jobList` 数组。缺少获取时间或查询上下文时保留未知，不用文件导入时刻补造观察时间。
- 事件JSON：含 `events` 数组；列表事件为 `kind=list_job`、`payload` 为原始列表项，详情事件为 `kind=detail`、`payload` 为详情 `zpData`。`captured_at` 保存实际获取时间，`query` 保存观察来源。源库已有数据可导出到此格式用于重放。

单独的详情JSON无法定义一轮列表样本，因此不直接创建采样批次。响应只有请求信息、没有正文的HAR也不能变出岗位数据。

## 命令

```powershell
uv run job-radar import-capture capture.har local/runs/imported.sqlite3 --round-id import-20260930
```

输出必须是新的独立数据库。执行过程写入 `.importing` 临时文件，完整性检查通过才转为目标文件；失败的中间文件保留，不覆盖源文件或运行库。导入批次状态为 `imported`；不会从几份响应推断整个查询已完成。

默认将详情记为访问未核验，不从HAR、正文长度或非空结果推断完整访问。只有对受控的、此前已核验的事件JSON，才能显式使用 `--preserve-verified` 保留来源声明；清单会标明来源声明的性质。HAR和原生API JSON不接受此选项。

批次导出复用 `export-round` 的实现。源数据库显式指定时可使用Python入口：

```python
from pathlib import Path
from job_radar.rounds import export_round
export_round(Path("local/runs/imported.sqlite3"), "import-20260930", Path("local/runs/imported-snapshot.sqlite3"))
```

清单保留 `capture_provenance`：文件SHA-256、导入时间、输入格式、核验声明和新增平台请求数0。JobHunt读取这层来源，文件导入不会被报告成新的在线抓取。

## 实际运行回执

已有源库的聚合查询来源及原始字段导出1086个事件：1080次列表观察、6份此前核验的详情。新入口导入到独立SQLite后为1024主键；完整性ok，列表时间没有改成导入时间。快照包含6份核验来源声明，查询完成数0；新旧对照仍为214个重现主键。源库和正式求职分析入口未替换。

此验证覆盖事件JSON的真实数据路径。当前没有实际HAR样本，因此HAR读取分支仍需真实文件验收。没有单元测试。
