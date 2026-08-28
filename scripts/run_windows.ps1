# 日常一轮采集（Windows）。等价于 python scripts/daily_cycle.py。
#
# 2026-08-11 重写：旧版引用 `config.yaml` 和 `job-radar` CLI，两者都已不存在
# （配置是 config.hangzhou.yaml，入口在 scripts/ 下），照着跑会直接报错。
#
# 用法:
#   .\scripts\run_windows.ps1              # 完整一轮
#   .\scripts\run_windows.ps1 -DryRun      # 只看计划不发请求
#   .\scripts\run_windows.ps1 -Pages 10    # 列表翻更深（挖被截断的宽泛词）
param(
    [switch]$DryRun,
    [int]$Pages = 5,
    [double]$Reserve = 0.15
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# 先报余量：封禁按单日累计请求量算，跑之前该知道还剩多少额度。
uv run python scripts/request_budget.py

$cycleArgs = @("--pages", $Pages, "--reserve", $Reserve)
if ($DryRun) { $cycleArgs += "--dry-run" }
uv run python scripts/daily_cycle.py @cycleArgs
