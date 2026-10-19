---
name: boss-refresh-chain
description: BOSS 状态刷新：判定只降不升、冷却期与 unknown 不进冷却、落库默认开、补录按原观测时间、成本靠列表复观测摊薄
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 状态刷新

两条入口，共用同一套判定：

| 入口 | 用途 | 编排 |
|---|---|---|
| `job-radar task start refresh` | 服务内任务（账本、节流、验证窗口） | `job_radar/service_jobs.refresh` |
| `local/analysis/live-20261018/refresh_batch.py` | 跳板批量，两条匿名出口并行 | `boss_scrape.refresh.run_batch` |

取值与判定在 `boss_scrape/refresh.py`（机制见 [[boss-scrape-layer]]），
选目标与回写在 `job_radar/refresh_targets.py`，落库走 `annotations.record_status`。

## 判定：不确定一律弃权

只把**明确的关闭文案**当关闭（`.job-status` 的值是「职位已关闭」等），
在招是「招聘中」/「最新」，其余一律 `unknown`。
方向是刻意的：误判关闭会单向降级一个在招岗位（不可逆），误判在招只是下一轮再刷（便宜）。
标题与请求的对不上、URL 被重定向、平台加了新文案，全部弃权。

## 四条不能改的行为

- **落库默认开**。只记 JSONL 而不写库等于没刷：上一版就是这么攒了 180 条记录、
  库里一条没变。`--no-write` 才关，`--replay <jsonl>` 按**原观测时间**补录（不发请求）。
- **成功检查才有冷却期**。3 天内查过（open/closed）的跳过；`unknown` 不进冷却，
  否则判不出来的那批会被永久漏掉。时间比较走 `julianday()`，不比 ISO 字符串
  （`isoformat()` 微秒为 0 时会省掉小数部分，`+` 比 `.` 小，同一秒内会颠倒；
  补录进来的历史时间还不带时区）。**冷却期写 0 表示不做冷却**，要把整条条件去掉，
  不能把 cutoff 设成「现在」—— Windows 系统时钟只有约 15.6ms 粒度，两次 `now()`
  可能同值，刚写进去的那条就会落在 cutoff 上被判成"刚查过"。
- **自愈必须开新页面**，且那次 `goto` 要记账。复用忙碌页面恢复不了，`recovered` 会永远 False；
  `recover_session` 的 `before_navigate` 钩子是给调用方先进账本再发请求用的，
  不报告就等于有请求绕过账本。
- **原始 HTML 不进库**。`evidence_for_store` 只留 `text_len` + `text_digest`，
  140KB × 数千条会把库撑爆。

## 判定「还在不在招」：列表出现不算数

**死岗位也挂在列表里**，所以 `last_seen_at` 只能证明"被搜到过"，不能证明在招。
实测 11,812 条里，真正能用的信号有三组，优先级从硬到软：

| 信号 | 字段 | 覆盖 | 含义 |
|---|---|---|---|
| 平台状态 | `job_status_desc` / `job_invalid` | 58.4% / 100% | 「招聘中 / 最新 / 职位已关闭」；716 条明确关闭 |
| HR 活跃度 | `recruiter_active_status` / `_days` / `recruiter_online` | 80.2% / 59.0% / 100% | 在线 27.0%、刚刚活跃 19.1%、今日活跃 9.6%、3日内 9.7%、**半年前活跃 3.9%** |
| 存在时长 | `first_seen_at` → `last_seen_at` | 100% | 同日 53.5%、1-7 天 18.8%、8-30 天 19.8%、31 天以上 7.9% |

折算成五档在 `analysis/market.py::hiring_of` 里做**一次**，前端只显示不重算：
`open`(61.3%) / `warm`(5.8%) / `cold`(0.2%) / `stale`(7.3%) / `closed`(6.4%) / `unknown`(19.0%)。

两个容易写错的地方：

- **days 与 text 可能不同步**（列表轮次和详情轮次各写各的），取两者中**更活跃**的那个，
  否则「HR 在线」但 days 停在 3 天前的行会被误判成 warm。
- **平台状态压过 HR 活跃度**：`job_status_desc=职位已关闭` 即使 HR 刚刚活跃也判 closed。

`seen_count` 是"次数"不是"天数"，会被同一天的多关键词扫描灌水，不能直接当存在时长用。

## 状态

待刷池 `active + valid` = 3,691（`job_invalid=1` 751，共 11,361 条）。
`last_seen_at` 分布偏旧：14 天内仅 63 条，30 天内 1,024 条，最大一桶是 2026-09-12 的 1,942 条。
在线验证的下一步见 [[boss-refresh-resume]]。

相关：[[boss-scrape-layer]] [[boss-risk-control]] [[boss-job-radar]] [[boss-data-and-filtering]] [[boss-refresh-resume]]
