# scripts/ 索引

37 个脚本按用途分组。`_archive/` 里的 71 个是一次性探针，结论已进记忆，留作证据不再维护。

判断一个脚本还能不能用，先看它属于哪条**链路**——两条链路的风控代价差 13 倍，
不能混用（详见本机共享记忆的 oss-job-radar 主题，该文档不随仓库分发）：

| | 采集链路 | 状态刷新链路 |
|---|---|---|
| 端点 | `/wapi/zpgeek/*.json` JSON API | `job_detail/*.html` **同源 fetch** |
| 一条 = 几次请求 | **1**（含重算 1.18） | **1.7**（含重算；整页加载曾是 102） |
| 登录态 | 带账号 cookie（郭辉，仅本机） | **必须免登录** |
| 出口 | 本机直连家宽 | 波尔 SOCKS 跳板 |

⚠️ **2026-08-30 起两条链路的单条代价已接近**（1.18 vs 1.7），不再是"差 13 倍"。
刷新侧原先走 `page.goto` 整页加载，实测**一条 102 次请求** ——
旧文档写的 14 是从一张 verify 页数出来的，低估 7 倍。改成同源 fetch 后降到 1.7，
因为 `fetch()` 不加载子资源。
**但风控主体仍然不同**（账号 cookie + 家宽 vs 匿名 + 机房 IP），
日线必须各算各的，脚本不能混用。

---

## 状态刷新（免登录，走波尔）

- **`exit_health.py`** — 体检出口：IP + 封禁状态 + 精确解封时间。**一条 curl，不起浏览器**。
  判据是 BOSS 自己的 `/wapi/zpsecureflow/forbidden-info`。查状态前先跑这个。
  ⚠️ 被限制期间**绝不要起浏览器去试**：每访问一次解封时间就重置成「此刻+24h」。
- **`refresh_status_loop.py`** — 主力。刷「岗位关闭」+「招聘者活跃度」，
  双出口轮换、`request_log` 按**实际发生**记账（fetch 1 次、stoken 重算 3 次）、
  `--daily-cap` 按额度自动截断条数。`--verify` 才会弹人工验证窗口。
  取值走匿名同源 fetch（判据在 `src/job_radar/refresh_fetch.py`），
  **自适应 stoken 窗口**：连打 N 条主动重算，撞挑战页就把 N 收一格。
  实测 30-40 条批次 **1.7 次请求/条、0 未判定**。
- `check_job_status.py` — 早期版本，走整页加载（**102 次/条，不要用来跑批**）。
  `refresh_status_loop.py` 复用了它的 `ACTIVE_DAYS` / `parse_active_status` / `_is_closed`，
  而且它是"真浏览器带 CSS 渲染"的对照口径，所以**别删**。
- `probe_page_waterfall.py` / `probe_html_fetch.py` / `probe_closed_truth.py`
  — 成本与判据的标定探针（2026-08-30）。前者量整页 vs 拦截 vs fetch 的请求数，
  后者用**平台自己的 SEO 在招页**当对照组验证关闭判据有没有区分度。
  原始数据在 `data/calibration/`。
- `manual_verify_proxy.py` — 单独弹一个走指定 SOCKS 出口的浏览器让人工过验证。

## 采集（带登录态，仅本机）

- `daily_cycle.py` — 日常一轮的编排入口
- `expand_list.py` — 列表翻页扩量
- `backfill_details.py` — 回填 JD 正文与活跃度（走 `detail.json`，一条 1 次请求）
- `backfill_activity.py` / `backfill_from_raw.py` — 从已存 raw_json 补字段，**不发请求**
- `enrich_company_info.py` — 工商信息富化，用独立 profile `.browser-profile-reg`
- `check_account_status.py` — 查账号是否被限制

## 配额与风控标定

- **`request_budget.py`** — 查今日请求账本余量。注意换算：抓 N 条详情 ≈ 1.2N 次请求
- `analyze_quota_curve.py` — code=37 密度曲线
- `calibrate_quota.py` / `probe_window_share.py` — 配额窗口标定
- `probe_ban_boundary.py` — **封禁边界探测，需用户明确授权**。
  08-11 用 `config.aggressive.yaml` 跑了 1025 次**未触发封禁**，
  所以那个数字是「上界未知」，不是安全线。别拿它背书别的链路。
- `sustained_run.py` — 长时间持续跑，产出 `data/calibration/sustained-*.jsonl`

## 数据处理与分析

- `clean_local_db.py` — 重跑分层（active / sample / purged）
- `extract_glm_tags.py` — LLM JD 标签提取。模型/网关读 `.env` 的 `LLM_MODEL`
  / `LLM_API_URL` / `LLM_API_KEY`；文件名和 DB 列名里的 `glm` 是历史遗留，
  与当前用的模型无关。⚠️ 网关在 Cloudflare 后面，请求必须带浏览器 UA
- `apply_glm_filters.py` / `extract_jd_labels.py` / `eval_match_quality.py`
- `analyze_jobs.py` / `analyze_jd_labels.py` / `analyze_keyword_yield.py`
  / `analyze_local_filter.py` / `analyze_tag_diffs.py`
- `mark_bookmarked.py` — 标记投递过的岗位（`bookmarked=1` 保护不被降级）

## 简历与产出

- `expand_resume.py` / `backfill_resume.py`
- `export_pool.py` + `serve_pool.py` — **投递池导出成单页 HTML** 并起本地服务。
  分桶（P0/P1/P2、高/低/超纲薪资、风险公司）是页内筛选而非文件切分，
  所以能跨桶搜索排序。页面模板在 `_pool_template.py`。
  跑服务时点「已投」会写库（`chat_actions` + `bookmarked`）；
  直接双击 HTML 打开则退化用 localStorage。
- `export_pool_html.py` — 旧的七文件导出，**已被 `export_pool.py` 取代**。
  保留是因为新脚本复用它的判定函数（`job_score`/`hard_drop`/`salary_bucket`
  /`active_tier`/`real_risk_tags`），那些口径全部经过实测校准，不重写。

## 辅助

- `fetch_credentials.py`、`keep_browser_open.py`、`listen_manual.py`、
  `pure_http_client.py`、`list_local_matches.py`
- `run_windows.ps1`、`start_chrome_windows.ps1`、`clash_pipe.ps1`

---

## 配置档对应关系

跑采集时选哪个 config 决定节流强度，**别照 aggressive 抄**：

| 档 | 日线 | detail 冷却 | 用途 |
|---|---|---|---|
| `config.hangzhou.yaml` | 600 | 90-150s | 最保守，日常 |
| `config.steady.yaml` | 1000 | 60-100s | 日常提速 |
| `config.aggressive.yaml` | 2000 | 20-40s | **仅边界探测**，需授权 |
| `config.smoke.yaml` / `trial` | — | — | 冒烟/试跑 |
