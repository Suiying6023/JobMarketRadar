# Job Market Radar

自用的 BOSS 直聘岗位采集与筛选工具。抓岗位列表和详情入本地 SQLite，用 LLM 读 JD
判断岗位是否够得着，导出成一个可浏览的投递池页面。

**这是一套由人（或 LLM）调控的工具集，不是一键跑完的流水线。** 脚本负责执行，
策略（跑多快、跑哪些、越过配额怎么办）一律通过启动参数表达。日常操作看
[`scripts/README.md`](scripts/README.md)，那里有全部脚本的索引和两条链路的对照。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

依赖以 `pyproject.toml` 为准。其中 **`patchright` 必装**——它是 playwright 的反检测
分支，`collectors/boss.py` 直接 import 它，缺了会在 import 阶段就 `ModuleNotFoundError`。

浏览器用**系统装的 Chrome**（配置里 `channel: chrome`），不需要
`playwright install chromium`。

本机环境由 `uv` 管理，命令前缀是 `uv run`：

```powershell
uv run python scripts/<脚本>.py --help
```

## 快速开始

复制 `config.example.yaml` 改成自己的城市与关键词，然后：

```powershell
uv run job-radar doctor  --config config.yaml   # 校验配置
uv run job-radar collect --config config.yaml   # 采集入库
uv run job-radar summary --config config.yaml   # 看统计
uv run job-radar export  --config config.yaml   # 导出 CSV/JSON
uv run job-radar report  --config config.yaml   # 生成 HTML 报告
```

首次 `collect` 会打开 Chrome 并复用 `.browser-profile` 里的登录态，需要先在那个
profile 里登录一次。输出落在 `data/`（SQLite）和 `output/`（CSV/JSON/HTML）。

## 两条采集链路

这两条链路的端点、登录态、出口 IP、风控主体**各自独立**，日线也各算各的。
混用脚本或合并计数会得出错误结论。

| | 采集链路 | 状态刷新链路 |
|---|---|---|
| 抓什么 | 岗位列表 + JD 详情 | 岗位是否关闭、招聘者活跃度 |
| 端点 | `/wapi/zpgeek/*.json` | `job_detail/*.html` |
| 登录态 | **带账号 cookie** | **必须免登录** |
| 出口 | 本机家宽 | Bohrium SOCKS 跳板 |
| 账本 `kind` | `list` / `detail` / `security_check` | `detail_page` |

**账号只在本机跑，不要带着登录态走跳板 IP**——那等于用陌生 IP 操作主账号。
两条链路的脚本都有守卫会拒绝这种组合。

## 配置档

`config.*.yaml` 是同一套结构的不同强度档，按需选，不要改代码：

| 档 | 日线 | 详情批 / 批间冷却 | 用途 |
|---|---:|---|---|
| `hangzhou` | 600（默认值） | 5 / 90-150s | 最保守 |
| `steady` | 900 | 6 / 60-100s | 日常 |
| `cities3` | 900 | 6 / 60-100s | 三城 × 27 词 |
| `aggressive` | 2000 | 8 / 20-40s | 边界探测，明知会触发风控 |
| `trial` / `smoke` | — | — | 试跑 / 冒烟 |

## 安全与自动化边界

- **自动开聊默认关闭**，需要显式开启并二次确认才会发出真实消息。
- 遇到平台验证或风控提示**立即停止，不重试**。
- 撞上人工验证时默认**不弹**浏览器窗口，只在显式加 `--verify` 时弹——
  没人在场的窗口零产出，还占着出口。
- 浏览器窗口默认落在主屏之外以免抢焦点。**坐标必须为正**：负坐标会被平台的挑战
  脚本判为异常环境，请求被推去登录页，那种窗口人是点不动的。

## 结构

```text
src/job_radar/
├── collectors/       平台适配器（boss.py 是采集主体，browser.py 管浏览器会话）
├── analysis/         岗位分类与技能抽取
├── profile.py        ★ 所有筛选阈值、名单、评分权重的单一真源
├── refresh_fetch.py  状态刷新链路的取值判据与成本常量
├── models.py         配置与数据模型（pydantic）
├── storage.py        SQLite 读写、字段保护语义、请求账本
├── normalization.py  文案→结构化（活跃度、薪资、学历）
├── runner.py         采集编排
├── report.py         HTML 报告
└── cli.py            命令行入口

scripts/              可复用工具集，见 scripts/README.md
tests/                回归测试，钉住实测校准出来的判据
data/calibration/     实测原始数据（探针产出）
```

改筛选阈值只改 `profile.py`；改节奏与配额只改 config 或启动参数。
