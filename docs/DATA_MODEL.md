# 数据口径与证据

## 运行与存储

`jobs` 保存来源岗位，`canonical_id` 是跨任务关联主键。服务运行库 `service-state.sqlite3` 单独保存任务、进度与游标日志，人工记录仍在 `review-state.sqlite3`。

实际数据任务按需创建以下补充表：

| 表 | 用途 |
|---|---|
| job_annotations | 标注输入快照、输出、模型/端点、配置版本、来源与提示词哈希、用量 |
| annotation_prompts | 按哈希保存提示词原文，供不同标注版本共用 |
| job_status_checks | 状态检查时间、任务、开放/关闭/未知结果与解析证据 |
| collection_rounds | 本轮查询计划、筛选规则哈希、开始时间、状态与最后执行任务 |
| collection_queries | 已完整执行的批次查询，用于续跑跳过；中途退出的查询重新执行 |
| collection_observations | 本轮实际出现的岗位、查询来源和当时的整行快照 |

标注按岗位、输入、prompt、端点和模型去重。原文或配置改变时旧标注仍保留，界面标为待更新。输入按配置截断时明确记录 `input_truncated`，完整正文参与来源哈希。

默认抽取输入附带带编号的原文片段。模型选择 `evidence_ids` 和 `requirement_note_ids`，程序按存在的编号回填 `evidence`、`requirement_notes`；不存在的编号拒绝入库，重复和超量的有效编号在保存时去重并限为8项。择一、优先和放宽条件保留原句，技术数组仍用于导航，不将全部列举项当作同时必需。

应届表态使用受限编码并映射为文本；原文未提及应届、校招或届别时只允许“未说明”。经验年限单独记录，不能改写成招聘方明确拒绝应届。旧提示词的结果按版本保留，新输入结构和新提示词会使旧标注待更新。
状态刷新仅在结果可判定时更新岗位关闭标记，JD 正文时间保持独立；只有成功的状态检查进入刷新冷却判断。

## 岗位池与可配置复核

全国概览统计完整 `jobs` 表，不先过滤 `active`、旧 LLM 标签或候选人城市。
个人复核另按 `candidate` 配置计算，三个或更多优先城市均同级加分。
“优先核实”是阅读顺序，不是录用概率，也不是今天仍在招的证明。

技能统计对正文做 Unicode 规范化和词边界匹配，分母是非空 JD 记录；词语可以多选，比例不能相加。
它是词项出现率，不是岗位真实建设 Agent 的语义分类。岗位标题另作互斥词类统计，不能替代职责判断。
平台行业字段通常描述公司行业；它不等于岗位所负责的具体业务。

采集关键词、城市、学历/经验筛选，以及详情补抓顺序都会影响样本。
这些数据适合比较已观察到的需求，不足以估计全国职位数量、市场占有率、招聘增速或成功率。

## 薪资、时间和重复

- 月薪比较：有效挂牌区间的中点，单位 K/月；统计输出明确给出有效薪资分母。
- 候选薪资匹配：挂牌区间与期望区间是否相交；高薪不直接等同经验不符。
- 月数/奖金缺失时不推年包；报价不是到手收入或录用薪资。
- `detail_fetched_at` 被历史详情与状态刷新共用，不保证正文也在该时刻更新；缺值表示来源时间未知。
- `updated_at` 裸时间按 SQLite UTC 解读；`request_log.ts/day` 是历史本地日历日。不能不换时区直接比较。
- “在线”“今日活跃”都是采集当时的字段，工作台不据此宣称当前在招。超过七天只提示复核，不删除岗位。
- `canonical_id` 是记录主键；公司标识+标题+城市相同只提示疑似重复，不自动合并。
- 新服务采集更新 `last_seen_at/seen_count`；详情补全保持它们不变。历史数据曾混入补详情时间，跨轮比较优先使用 `collection_observations`。
- `matched_queries_json` 是跨历史轮次累计的关键词；本轮来源使用 `collection_observations.queries_json`，不能用累计词表替代。
- 公司数按去空白的公司名称字符串计数，不等于企业法人数量；别名、同名和匿名主体都会影响它。
- 规范化正文相同但公司名不同，可能源于同集团、渠道或模板；不据此判企业虚假。

## 状态与数据分离

| 文件/表 | 作用 | 工作台行为 |
|---|---|---|
| `jobs` | 原始岗位事实、正文、来源与历史标签 | 只读 |
| `keyword_progress` | 历史城市×关键词断点 | 只读展示；英文大小写合并仅用于覆盖统计 |
| `request_log` | 请求尝试及结果 | 只读展示；采集器原子预留并按自己的行 ID 回填 |
| `collection_runs` | 运行统计与停止原因 | 采集结束时记录 |
| `cross_platform_groups` | 历史候选分组 | 悬空项只报告，不自动删原数据 |
| `review-state.sqlite3` | 本地收藏、已投、备注及操作记录 | 单独写入 |
| `access-policy.json` | 当前本地访问状态 | 网页只读，命令行人工管理/采集器风险暂停 |
| `config.local.yaml` | 采集计划与候选画像 | 校验后保存，保留历史版本 |

旧 `glm_tags_json` 是历史模型意见，默认空对象也不是已完成标注；不要直接把 `keep` 或 `active`
当作可靠可达性结论。旧标签的提取模型、提示词口径和时间标记可能不同，本版不覆写这批证据。

备份请用 `job-radar db-backup`，它调用 SQLite backup 并执行完整性检查、输出 SHA-256 清单。
原始表保留，质量问题和派生排序分开管理，便于以后用新证据重新判断。

refresh-access-policy.json 保存刷新链路的独立暂停；主访问策略仍控制全部平台任务。浏览器中残留 Cookie 不能证明服务端登录仍有效。实际完整正文与非空预览正文必须分别核实。

raw_json.detail_access_verified 标记本轮详情访问核验状态：true 才进入本轮模型抽取；false 进入重新获取队列。缺失代表历史未记录，不自动升格。detail_preview 保存可识别的预览正文和获取时间，正文完整版本与其时间分开保留。较短的已核验新版正文仍可更新。
## 本轮正文来源

`raw_json.detail_source` 保存新解析器处理的详情来源：岗位ID、获取时间、正文SHA-256和访问核验状态。解析成功本身不证明完整访问；既有数据没有此字段时不补造来源证据。

`detail_fetched_at` 是所保存正文的获取时间，列表观察时间单独保留。`detail_access_verified` 为未知或false时，近期非空正文仍可进入详情补取队列。新批次快照包含 `collection_rounds`、`collection_queries`、`collection_observations`；清单的 `body_quality` 区分本轮核验正文、继承/时间未明核验正文、访问未核验非空正文和缺失正文。

## 决策模型标签列

`job_analysis.relabel` 用决策模型给每条有正文的岗位打 8 个标签，写入下列列。
`relabeled_at` 非空即表示这组标签来自当前 schema；`relabel_model` 记录模型名
（失败时是 `<model>:failed`）。

| 列 | 类型 | 取值 | 含义 |
|---|---|---|---|
| `role_family` | TEXT | `ai_app` / `algorithm` / `backend` / `fullstack` / `data` / `test` / `frontend` / `ops` / `embedded` / `product` / `operation` / `sales` / `design` / `management` / `content` / `teaching` / `ecommerce` / `implementation` / `other` | 岗位族，按**正文实际职责**判，不受标题影响 |
| `role_confidence` | REAL | 0–1 | 上面那一项的置信度 |
| `role_probabilities` | TEXT | JSON | 全部分布，便于按置信度过滤 |
| `title_mismatch` | REAL | 0–1 | 标题方向词与正文职责不一致的概率 |
| `seniority_score` | REAL | 0–4 连续值 | 0=实习应届、1=初级 1-3 年、2=中级 3-5 年、3=高级 5-8 年、4=专家管理 |
| `employment_type` | TEXT | `fulltime` / `internship` / `parttime` / `campus` | 用工性质 |
| `fresh_grad` | TEXT | `accept` / `maybe` / `reject` / `unstated` | 对应届生的表态。**`unstated` 不是拒绝** |
| `grad_year` | TEXT | `2026` / `2027` / `other_year` / `any_year` / `unstated` | 限定届别 |
| **`education_req`** | TEXT | `phd` / `master` / `bachelor` / `college` / `any` / `unstated` | **正文的学历门槛**。不能用列表的 `education` 代替 |
| `is_proxy` | REAL | 0–1 | 外包 / 劳务派遣 / 中介代招的概率 |

⚠️ **为什么学历要单独一列**：列表的 `education` 字段是**搜索条件的回显**，不是
岗位门槛。实测某岗位列表标「本科」（因为搜索时按本科筛），正文写的却是
「2026.9-2027.8 期间已获得或即将获得**博士/硕士**学位的同学」。只看列表会把
硕博岗放进本科可投清单。

⚠️ **`education_req` 有版本断层**：它是 2026-10-19 新加的 schema 问题，只有按
7 天窗口重标的 318 条有值（4.2%），更早的 7,184 条是旧 schema 产物、该列为空。
判空要按「只有新数据才有」理解，不能当成「这个岗位不提学历」。

`tech_stack_json` 是**规则抽取**（`job_analysis/tech.py` 的词表匹配），不是模型产出：
`{"by_category": {"语言": ["Python"], "AI工程": ["RAG"]}, "flat": ["Python", "RAG"]}`。
技术词是封闭集合，规则更准、更便宜、可审计。

## 工商登记列

`company_reg_json` 存 GSXT 照面的简版，来自**详情页的 `.business-info-box`**，
跟着 JD 一起抓、零额外请求：

```json
{"公司名称": "…", "法定代表人": "…", "成立时间": "2019-10-30",
 "企业类型": "有限责任公司（法人独资）", "经营状态": "存续", "注册资本": "100万人民币"}
```

⚠️ 页面上的标签是「**注册资金**」，统一归一成「注册资本」，与
`enrich_company_info.py` 从 `/gongsi/` 抓的完整版（含统一社会信用代码、注册地址）
共用同一套键名。

⚠️ 列默认值是 `'{}'`（长度 2），用 `<> ''` 判空会把空对象误算成「有值」——
判有没有真数据要 `length(company_reg_json) > 2`。

`company_reg_updated_at` 是这组工商数据的写入时间。

