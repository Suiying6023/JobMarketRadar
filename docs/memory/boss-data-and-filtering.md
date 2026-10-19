---
name: boss-data-and-filtering
description: BOSS 数据所有权与消费者：运行源库、JobHunt 只读快照、岗位主键、时间薪资口径、通用评分与个人分析分工
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 数据与筛选

源库 `D:\Code\Boss\data\hangzhou_agent.sqlite3` 由 BOSS 服务维护，实际是全国多城市采样池。
文件名与采集关键词不能代表完整全国招聘市场。字段口径以 `D:\Code\Boss\docs\DATA_MODEL.md` 为准。

## 源库、快照与使用结果

个人分析从 `D:\Code\JobHunt\data\sources.json` 读取独立只读快照；manifest 记录 SQLite backup 方法、
来源时间、行数与哈希。求职 agent 在 JobHunt 保存分析代码和结果，运行源库保持由数据服务管理。
快照引用 `jobs.canonical_id` 关联岗位，旧报告分别保留日期和分母。

岗位池总览及通用画像/评分可在 BOSS 保存；个人画像研究与建议归 [[job-hunt-workspace]]。
全国层从完整 jobs 开始，不让个人城市、retention_status、旧模型 keep/demote 缩小初始分母。
个人层结合用户事实、岗位原文和配置计算参考队列；评分表示规则匹配程度，录用与在招状态需要实际证据。

## 数值与时间

- 技能提及统计以非空 JD 为分母，Unicode 形似字和英文大小写统一；词语提及率与语义职业分类分别解释。
- 同文检测另有最少正文长度条件；跨公司同文可能是集团、代招或模板，需进一步核实。
- 薪资使用挂牌月薪区间；薪资月数、奖金和总包单列，缺值保留缺失。高薪本身不等于年限不符。
- `detail_fetched_at` 被历史详情与状态刷新共用，不能直接判断正文同步更新；缺值仅说明记录缺失。
- `updated_at` 裸格式是 SQLite UTC；request_log 的历史 ts/day 是本地日历日，比较前统一时区。
- 旧 glm_tags_updated_at 有本地时间误标 Z 的批次，应按具体来源处理。

## 库体积与剪枝

源库约 110MB / 11,812 条岗位，`raw_json` 合计 14.9MB。

- **`raw_json.detail` 已整体剪掉**（13.2MB / 6,805 行）。它是详情接口的整包，而里面每个有用字段
  都已落成列（position_name / job_status_desc / address / recruitment_count / JD / company_intro），
  全仓 grep 确认没有读取方——只有 `boss_payload` 写入、`storage` 在 upsert 时看新载荷里的这个键。
  剪枝记录写在 `raw_pruned_keys`（标记 `raw.detail@whole`）与 `raw_pruned_at`。
  剪枝前的整库留在 `data/backups/*-pre-prune.sqlite3`。
- **`raw_json.securityId` 在 2026-10-18 15:42 的一次性剪枝里被删掉了**（11,329 行），
  那批岗位无法再走 `detail.json` 补正文。备份里只剩 90 条，任何快照都救不回来。
  但**详情页 HTML 里内联的 `var _jobInfo = {...}` 自带 `securityId`**——打开页面就能就地取到，
  所以这不是死路：详情改走 SSR（见 [[boss-scrape-layer]]），或者先开页面取 securityId 再打接口。
- **`collection_observations.row_json` 不要剪**：它存的是整条岗位快照，`rounds.py` 的轮次导出要用。
- `cross_platform_groups` 12,431 行但 `platforms` 只有 boss，实际是每个岗位映射到它自己的
  1:1 表，零信息量；`collection_runs` 0 行。两张都可以删。
- 单靠 VACUUM 只能省 4.8MB（碎片不严重），体积的大头是重复载荷，不是空洞。

## 字段性质分层

`jobs` 现在 80 列，按来源分四类：raw（平台原值）、derived（LLM/规则产出，
**只能存一个版本**）、state、key/manual。派生列填充率卡在 `job_description`
非空的行数上 —— "没正文就不打标"，合理但没写在任何地方，只能从数字反推。

有正文 7,502 条的**基础标签**（`role_family` / `seniority_score` / `fresh_grad` /
`grad_year` / `employment_type` / `is_proxy` / `tech_stack_json`）覆盖率 **100%**。
（此前只有 62.8%，是因为新抓的岗位从没跑过打标。）

⚠️ **`education_req` 只有 4.2%（318/7,502）**。它是 2026-10-19 新加的 schema 问题，
只有按 7 天窗口重标过的那批有值；更早的 7,184 条是旧 schema 打的，没有这一列。

这带来一个真实后果：**老的硕博岗不会被 `education_req` 拦住**。要么接受这个缺口、
只用它筛新数据，要么显式跑 `--max-age-days 0` 全量重标（约 78 分钟模型调用）。
清单里同时保留了列表 `education` 字段的检查作为粗兜底，但那个字段是搜索条件回显、
不可靠（见 `docs/PLAYBOOK.md` 第 7 节），不能替代。

`company_reg_json` 挂在岗位行上，同一家公司平均被复制 1.71 份（最多一家 71 份）。
公司层数据应该抽成独立表，见 [[boss-company-data]]。

## 判定规则：LLM 标签做主

`analysis/decisions.py::evaluate()` 的判据分三层：

1. **LLM 标签**（`role_family` / `seniority_score` / `fresh_grad` / `grad_year` /
   `education_req` / `employment_type` / `is_proxy`）—— 主要判据
2. **规则抽取**（`tech_stack_json`）—— 用来区分「AI 开发」和「AI 数据标注」
3. **结构化数据**（城市、薪资、在招状态、工商注册资本、公司名）—— 兜底

**不用正则解析 JD 做分类**。早前每加一条正则都是给上一个漏判打补丁，而正则
只能覆盖想得到的情况，想不到的全漏。详细规则与实测阈值见 `docs/PLAYBOOK.md`
第 7 节。

`profile.CITY_WHITELIST` / `city_allowed()` 已删除 —— 它和 `CandidateProfile.cities`
是两套相反语义（一个缺省白名单、一个空列表=不限），城市的唯一真源是
`config.local.yaml` 的 `candidate.cities`。

## 证据与通用存储

原始 JD、历史模型意见、评分理由和人工记录分别保留来源。工作台手工状态当前在 Boss/data/review-state.sqlite3，
旧 chat_actions 保留在源库。模型列名 glm 不限定当前使用的模型。
品牌 ID（缺时公司名）+标题+城市只提示疑似重复；公司名称计数不等同企业法人数量。
一致性备份使用 SQLite backup；采集原文、旧标签与历史关联用于追溯。

## 服务补充表

job_annotations 保存模型输入快照、输出、来源哈希和版本；annotation_prompts 按哈希保存提示词。
job_status_checks 保存独立状态检查和证据。原文或配置变化后，旧标注显示为待更新；
未知状态仅记证据，成功状态检查才进入刷新冷却。任务状态和事件在独立 service-state.sqlite3。

相关：[[boss-job-radar]] [[job-hunt-workspace]] [[boss-keywords-and-tagging]] [[user-profile]]
