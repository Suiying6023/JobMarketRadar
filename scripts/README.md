# scripts/ 索引

60 个脚本按用途分组。`_archive/` 里的 74 个是一次性探针，结论已进记忆，留作证据不再维护。

**这些脚本是可复用工具，策略走参数。** 每次任务的差异（跑多快、跑哪些、越过配额
怎么办、要不要弹验证窗）用启动参数表达，不要改代码。拿不准就先 `--help`。

## 先分清链路

两条链路的**风控主体不同**（账号 cookie + 家宽 vs 匿名 + 机房 IP），
所以日线各算各的，脚本不能混用。

| | 采集链路 | 状态刷新链路 |
|---|---|---|
| 端点 | `/wapi/zpgeek/*.json` JSON API | `job_detail/*.html` 同源 fetch |
| 登录态 | 带账号 cookie（**仅本机**） | **必须免登录** |
| 出口 | 本机直连家宽 | 波尔 SOCKS 跳板（1080 / 1082） |
| 账本 `kind` | `list` / `detail` / `security_check` | `detail_page` |
| 一条详情的账面成本 | 约 1.5 次请求 | 约 1.6 次请求 |

⚠️ **账面成本不等于真实上网次数**。采集侧每次 stoken 重算真实发约 37 次请求，
而账本按设计只记 1 行（闸门按账面口径设，真实成本另行标定）。
所以拿账本数字估「打了平台多少次」会低估一个量级——要估真实压力看
`data/calibration/recover-cost-*.json`。

## 状态刷新（免登录，走波尔）

- **`exit_health.py`** — 体检出口：IP + 封禁状态 + 精确解封时间。**一条 curl，
  不起浏览器**，判据是 BOSS 自己的 `/wapi/zpsecureflow/forbidden-info`。查状态先跑它。
- **`refresh_status_loop.py`** — 主力。刷「岗位关闭」+「招聘者活跃度」，
  双出口轮换、按实际发生记账（fetch 记 1、stoken 重算记 3）。
  常用参数：`--skip-fresh-hours`（跳过刚刷过的行）、`--over-quota stop|slow`
  （越过日线是停还是降速）、`--only-exit`（避开坏出口）、`--verify`（弹人工验证窗，
  **默认不弹**）、`--window-position`（窗口落点）。
- `check_job_status.py` — 早期版本，走整页加载（约 102 次/条，**不要用来跑批**）。
  `refresh_status_loop.py` 复用它的 `parse_active_status` 与 `_is_closed`，
  而且它是"真浏览器带 CSS 渲染"的对照口径，所以**别删**。
- `manual_verify_proxy.py` — 单独弹一个走指定 SOCKS 出口的浏览器让人工过验证。
- `watch_chains.py` — 巡检：两条链路的存活、风控信号、额度、库存一次输出。
- `cleanup_browsers.py` — 清掉强杀后遗留的孤儿浏览器。默认只报告，`--kill` 才动手。
  **强制停止跑批后一定要跑一次**：`Stop-Process -Force` 不给 finally 机会，
  遗留的浏览器会继续自己重试验证，而那些请求不进任何账本。

## 采集（带登录态，仅本机）

- `daily_cycle.py` — 日常一轮的编排入口
- `overnight_supervisor.py` — 无人值守调度器，按 `--stages` 串起多个阶段并在
  撞风控时停机（写 `logs/SUPERVISOR-STOPPED.txt`，不自动重试）
- `expand_list.py` — 列表翻页扩量
- `backfill_details.py` — 回填 JD 正文与活跃度（走 `detail.json`）。
  选行前会过投递池判定，避免把配额花在注定被降级的岗位上
- `backfill_activity.py` / `backfill_from_raw.py` — 从已存 `raw_json` 补字段，**不发请求**
- `enrich_company_info.py` — 工商信息富化，用独立 profile `.browser-profile-reg`
- `check_account_status.py` — 查账号是否被限制，撞风控时弹窗给人看

## 配额与风控标定

- **`request_budget.py`** — 查今日请求账本余量。**跑采集前先看这个**
- `analyze_quota_curve.py` — code=37 密度曲线
- `calibrate_quota.py` / `probe_window_share.py` — 配额窗口标定
- `probe_recover_cost.py` / `probe_recover_callback.py` — stoken 重算的**真实**
  上网次数标定（就是上面那个 37 的来源）
- `probe_ban_boundary.py` — **封禁边界探测，需用户明确授权**。
  08-11 用 `config.aggressive.yaml` 打到 1025 次未触发封禁，所以那个数字的含义是
  「上界未知」而不是安全线，别拿它给别的链路背书
- `sustained_run.py` — 长时间持续跑，产出 `data/calibration/sustained-*.jsonl`
- 其余 `probe_*.py` — 一次性判据探针（挑战页真相、fetch 窗口、登录墙、
  页面瀑布流、静默模式可行性等），原始数据都在 `data/calibration/`

## 数据处理与分析

- `clean_local_db.py` — 重跑分层（active / sample / purged）
- `extract_glm_tags.py` — LLM JD 标签提取。模型与网关读 `.env` 的
  `LLM_MODEL` / `LLM_API_URL` / `LLM_API_KEY`。⚠️ 网关在 Cloudflare 后面，
  请求必须带浏览器 UA，否则回的是网关包装的 403 而不是 CF 挑战页——看着像上游挂了。
  文件名与 DB 列名里的 `glm` 是历史遗留，与当前用的模型无关
- `apply_glm_filters.py` / `extract_jd_labels.py` / `eval_match_quality.py`
- `analyze_jobs.py` / `analyze_jd_labels.py` / `analyze_keyword_yield.py`
  / `analyze_local_filter.py` / `analyze_tag_diffs.py`
- `mark_bookmarked.py` — 标记投递过的岗位（`bookmarked=1` 保护它不被降级）

## 简历与产出

- `expand_resume.py` / `backfill_resume.py`
- `export_pool.py` + `serve_pool.py` — **投递池导出成单页 HTML** 并起本地服务。
  分桶（P0/P1/P2、薪资档、风险公司）是页内筛选而非文件切分，所以能跨桶搜索排序。
  模板在 `_pool_template.py`。跑服务时点「已投」会写库（`chat_actions` + `bookmarked`）；
  直接双击 HTML 打开则退化用 localStorage
- `export_pool_html.py` — 旧的七文件导出。**保留是因为 `export_pool.py` 复用它的
  判定函数**（`job_score` / `hard_drop` / `salary_bucket` / `active_tier`
  / `real_risk_tags`），那些口径经过实测校准，不重写

## 辅助

- `fetch_credentials.py`、`keep_browser_open.py`、`listen_manual.py`、
  `pure_http_client.py`、`list_local_matches.py`
- `run_windows.ps1`、`start_chrome_windows.ps1`、`clash_pipe.ps1`

---

## 配置档强度

跑采集时选哪个 config 决定节流强度，**别照 aggressive 抄**：

| 档 | 日线 | detail 批 / 冷却 | 关键词 | 用途 |
|---|---:|---|---:|---|
| `config.hangzhou.yaml` | 600（默认值） | 5 / 90-150s | 13 | 最保守 |
| `config.steady.yaml` | 900 | 6 / 60-100s | 27 | 日常 |
| `config.cities3.yaml` | 900 | 6 / 60-100s | 27 × 3 城 | 多城扩量 |
| `config.aggressive.yaml` | 2000 | 8 / 20-40s | 13 | **仅边界探测**，需授权 |
| `config.trial.yaml` / `smoke` | — | — | — | 试跑 / 冒烟 |

`hangzhou` 档没写 `max_requests_per_day`，走 `models.py` 的默认值 600。
