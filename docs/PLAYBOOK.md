# 操作经验（PLAYBOOK）

**这份文档和 `docs/` 下其他文件的区别：**

- `ARCHITECTURE.md` / `DATA_MODEL.md` / `SERVICE_API.md` —— **代码怎么写的**
- 这份 —— **怎么跑、怎么判、哪里会骗你**

里面的每一条都来自实测，标注了日期和样本量。数字会过期，所以**每个结论都写清
"在什么条件下成立"**；条件变了就重新量，不要照搬。

---

## 1. 两条链路，别搞混

| | 采集链路 | 刷新链路 |
|---|---|---|
| 入口 | `session --phase calibrate/sweep/backfill`、`task start collect` | `task start refresh` |
| 浏览器 profile | `.browser-profile` | `.browser-refresh` |
| 出口 | `browser.proxy_server`（默认空 = **直连本机家宽**） | `refresh.proxy_server`（默认 `socks5://127.0.0.1:1080`） |
| 代码 | `BossCollector` / `RecommendationCollector` | `service_jobs.py::refresh` → `boss_scrape/refresh.py` |
| 熔断作用域 | 默认 | `AccessGate(scope="refresh")` |
| 干什么 | 发现新岗位、抓正文 | 复查**已有**岗位是否还在招 |

**为什么必须分清**：两条链路的风控阈值、出口 IP、会话状态都不一样。
2026-10-19 我把刷新链路的「约 86 请求/IP」和「10–16 秒自愈」直接套到采集链路上，
两个都不成立（见第 4 节）。记忆里那句「机房 IP 会被直接挑战，**家庭 IP 健康**」
才是采集链路的前提。

---

## 2. 速率：怎么算，怎么改

目标速率由**三个参数**共同决定，缺一个都算不准：

```
每条详情 ≈ 基础延迟中点 + 冷却中点/detail_batch + 突发中点/burst_threshold
```

当前 `config.local.yaml`：

| 参数 | 值 | 含义 |
|---|---|---|
| `detail_delay_seconds` | `[6, 11]` | 每条之间的基础等待，中点 8.5 |
| `detail_batch` | `8` | 每 8 条详情插一次冷却 |
| `detail_cooldown_seconds` | `[42, 62]` | 冷却，中点 52 |
| `burst_threshold` | `20` | 每 20 个请求插一次突发停顿 |
| `burst_penalty_seconds` | `[45, 75]` | 突发停顿，中点 60 |

```
8.5 + 52/8 + 60/20 = 8.5 + 6.5 + 3.0 = 18.0 秒/条 → 200 条/小时
```

离线模拟（3000 条 × 4 轮）实测 **201 条/小时（区间 200–203）**。
模拟脚本：`local/analysis/live-20261019/_simulate_pacing.py`（不联网、不 sleep）。

**改参数前先跑模拟**。`detail_batch` 大小**不改变峰值速率** —— 基础间隔始终 8.5 秒，
冷却只是把平均值压低。所以「调大 batch 提速」的实质是「减少停顿次数」，
不是「跑得更密」。

### 抖动：`throttle.lognormal_pause()`

不是固定间隔，也**不是**裁剪正态（那没有长尾，上界就是 `hi`，一眼看出是机器）。

现在用**对数正态**：`median = sqrt(lo*hi)`、`sigma = ln(hi/lo)/2`、上限 `8×hi`。
参考 `tr4m0ryp/Linkedin_Automation` 的 `humanizer/delay.rs` —— 同样的
`mu = ln(median)` + sigma 控尾部，外加 clamp 防单次抽到极端值把整轮拖死。

`[6,11]` 实测（50,000 次）：均值 8.63、**中位 8.11**、最小 6.00、p95 13.3、
p99 16.3、最大 28.3。**下限严格等于 `lo`。**

⚠️ 踩过两次：
1. 第一版按「相对中点的倍数」实现，`[6,11]` 抽出过 **5.04 秒** —— 均值守住了
   但下限被悄悄改掉。地板是运维设的安全参数，不能被抖动吃掉。
2. 第二版用 80/15/5 手工混合，参数是为了凑均值反推的，改一个要重算三个，
   而且最坏抽到 **255 秒**。

### 另外两层自适应（`pacing.RequestPacer`）

| 层 | 机制 | 作用 |
|---|---|---|
| 内容停留 | 读完 JD 后按字数微调 ×0.85–1.50，字数封顶 4000 | 取 Nielsen 实测「用户平均只读网页 **20%** 的文字」 |
| 挑战自适应 | 命中挑战 ×2（上限 16×），成功 ×0.85 衰减 | 借 `guise_pacing` 的 `MAX_CHALLENGE_MULTIPLIER=16`：「被站点推回时变慢」 |

放大快、衰减慢 —— 被推回要立刻让路，恢复要谨慎。三层叠加后速率仍由
基础间隔和批间冷却决定，后两层只改形状。

---

## 3. 详情抓取：三条路径并存

`detail_transport` 三选一，**默认 `ssr`**。三条都保留，用途不同：

| 值 | 实现 | 每岗位请求 | 依赖 | 适用 |
|---|---|---|---|---|
| `ssr` | 整页导航 + 解析 `.job-sec-text` | 1 次**整页文档**（含 JS，约 465KB） | 无 | 存量补正文、覆盖无 `securityId` 的岗位 |
| `api` | 页面内 `fetch(job/detail.json)` | 1 次接口调用 | 列表响应里的 `securityId` | 列表扫描时就地补详情（更轻） |
| `native` | 点搜索结果卡片再拦 `detail.json` | 1 次接口调用 | 卡片必须在可视区 | 只在页面结构验证时用 |

**`api` 的代价**：`securityId` 会被剪枝/轮换，列表翻几页后就可能失效；而且
`api` 路径下**一个岗位只发一个接口请求**，比 SSR 的整页加载轻一个数量级。

**待验证的问题**：两条路径的风控强度差异目前**没有对照数据**。
`sweep` 用的 `detail_limit` 会把详情和列表混在一起，看不出单条路径的成本。
要做对照，需要固定同一批岗位、分别用两种 transport 跑一遍，比较：
请求数、失败归因分布、`code=37` 出现位置、整页字节数。

现成工具：`local/analysis/live-20261019/_probe_stoken_loop.py`（N 次导航 + 列表复检，
`--no-roll` 可复现 stoken 过期）。

### ⚠️ SSR 导航之后必须补滚 stoken

**这是 2026-10-19 排查出来的，之前完全没有滚动点。**

整页导航会轮换 cookie 里的种子（`__zp_sseed__` / `__zp_sname__` / `__zp_sts__`），
但**不会重算 `__zp_stoken__`**；而 `StokenRoller` 只在「接口响应成功之后」才滚动。

A/B 实测（6 次导航 + 1 次列表复检）：

| | 导航结果 | 复检列表 |
|---|---|---|
| 不补滚（旧行为） | 4/6 成功，第 5 次起被重定向到 `zhipin.com/?_security_check=1_…` | **`code=37`** |
| 导航后补滚 | **6/6 成功** | **`code=0`** |

所以 `code=37`「您的环境存在异常」**不一定是封禁，很可能是 token 过期**。
遇到 37 先看浏览器里页面是否正常 —— 正常就说明是 token 问题，不是账号问题。
（补滚是纯 JS 计算，成本 0 个请求。）

### 详情页是 SSR + hydration

1. **只放行 document**。不 abort 子资源时，一次详情导航会发 **24 个 `/wapi/` 引导请求**。
2. **不依赖 `securityId`**，所以能覆盖「库里有 job_url 但没有 securityId」的存量岗位。
3. **登录态从页面内联的 `_userInfo.isLogin` 读**，零额外请求。

### ⚠️ 翻页与抓详情不能交错

`_collect_ssr_detail` 会 `page.goto` 到详情页，**离开推荐流/搜索页**。之后再翻下一页，
`window.scrollTo` 就滚在详情页上，等不到列表响应 → 30 秒超时。

**正确顺序：先把页翻完（页面全程停在列表上），再逐条抓详情。**
`session.py` 的 `calibrate` 阶段就是按这个写的，有回归测试锁着。

### 空正文要分清是哪一种空

`_ssr_blank_reason()` 分三层判据，**从硬到软**：

1. **`code-block` 结构**（最硬）：`403.html` 里内联多个块，只有带 `show` 的生效
2. **页面文案**：`IP存在异常` / `账户存在异常` / `访问过于频繁` …
3. **模型兜底**：规则都认不出来时，把可见文本丢给决策模型判

```
IP地址存在异常   → ip_restricted,          network_restriction, 35
账户存在异常     → account_restricted,     account_restriction, 32（403页）/36（verify页）
环境存在异常     → environment_restricted, environment_restriction, 37
访问过于频繁     → rate_limited
请先登录/未登录  → login_wall,             authentication_required, 7
```

**为什么要有模型这一层**：2026-10-20 那次，规则的四层判据全部漏掉同一个封禁页 ——
URL 不认识 `403.html`、字节数（44KB）落进 JS 挑战页区间（25–62KB）、
文案模式要求「地址」二字而平台写的是「您的 IP 存在异常行为」、
code 编号被页面里第一个块带偏。每补一次规则都要等下一次事故来验证。

**分工**：规则管它擅长的（`show` 标记、`expire-time` 这种机械提取、零成本），
模型管语义（「这是什么页」）。规则先跑，认不出来才叫模型 ——
正常下线的岗位很多，不能每个都问模型。

⚠️ **模型失败必须退回规则**，不能把「模型没答上来」当成「页面没问题」——
那等于给一个可能已封禁的账号开继续请求的口子。

⚠️ 认不出来的页面会落盘到 `local/abnormal/<时间>-<kind>.{html,txt}`，
供人事后复核。规则判错时光看 `request_log` 查不出真相 —— 真相在页面文字里。

非风险类的归因：

| 归因 | 含义 | 处置 |
|---|---|---|
| `verification` | 要滑块验证 | **停**，等人 |
| `js_challenge` | JS 挑战，偶发 | 标脏页面 + 通知 pacer 放慢 |
| `closed` | 岗位真下线 | 正常，跳过 |
| `selector_miss` | 页面在但没容器 | 我们的 bug，要查 |
| `empty_page` | 页面几乎空白 | 瞬时，可重试 |
| `unknown_page` | 模型也判不出来 | **停**，看落盘的页面 |

`_page_prompt()` 剥掉 script/style 取可见文本，**没命中风控词时返回空串** ——
正常岗位页的首句是 JD 正文，当成「平台提示」写进异常消息只会误导。

**按字节数交叉验证**（来自历史标定）：下架 ~11KB / JS 挑战 ~41KB / 验证页 ~44.5KB /
正常岗位页 **138–147KB**。2026-10-19 实测 `security.html` 是 42,825 字节，
正落在 JS 挑战页区间。

### 工商信息跟着 JD 一起白拿

`.business-info-box` 就在详情页里，**零额外请求**：

```html
<div class="detail-section-item business-info-box">
  <ul class="level-list">
    <li class="company-name"><span>公司名称</span>深圳昼光科技有限公司</li>
    <li class="company-user"><span>法定代表人</span>杨超</li>
    <li class="res-time"><span>成立日期</span>2019-10-30</li>
    <li class="company-type"><span>企业类型</span>有限责任公司（法人独资）</li>
    <li class="manage-state"><span>经营状态</span>存续</li>
    <li class="company-fund"><span>注册资金</span>100万人民币</li>
  </ul>
</div>
```

⚠️ **页面写的是「注册资金」不是「注册资本」**。早期按后者 grep 一直是 0 次，
于是误以为详情页没有工商信息、要单独请求 `/gongsi/{id}.html`。

⚠️ `company_reg_json` 这个列 2026-10-19 之前**不在 `JobRecord` 里**，`upsert()`
从不写它，只有旧的 `enrich_company_info.py` 用裸 SQL 维护 —— 覆盖率长期停在 4%
（727/11,958）。现在走正常链路，并且归入 `KEEP_IF_EMPTY_COLUMNS`（列表轮次的
`{}` 不能覆盖详情拿到的真数据）。

---

## 4. 风控：已知信号与判据

### 现状（2026-10-19 实测）

浏览器探针（`local/analysis/live-20261019/_probe_risk_state.py`，只发 1 次导航、
之后被动读 `page.url`，**零额外请求**）连测两次，结果一致：

```
导航 → security.html（43,417 字节）
被动等 2 秒 → 地址自己跳回 job_detail/...?_security_check=1
自愈后 465–467 KB，.job-sec-text 出现
```

**关键结论：挑战页是「读早了」，不是「被拦了」。**

详情页的 SSR 文档拿到后，站点自己的 JS 还要跑一段安全校验，通过后才跳回岗位页。
`wait_until="domcontentloaded"` 只保证 DOM 就绪，此时 `response.text()` 读到的是
校验**中途**的那一页 —— 约 41–43KB、没有 `.job-sec-text`。

正确做法是 `_settle_challenge()`：发现落在校验页就**只等不重发请求**
（`page.url` / `page.content()` 都是本地状态），实测 2 秒恢复。
唯一的网络流量是站点自己发起的那次跳转，它本来就会发生。

### ⚠️ 「屏外窗口一直不跳转」不成立于这个场景

`boss_scrape/refresh.py:292` 原文是「屏内窗口 10–16 秒自愈，**屏外窗口一直不跳转**」，
但那是描述 `refresh.recover_session` 里「新开页面 goto」的流程。
**直接导航的详情页在屏外窗口下 2 秒就自愈了**（两次独立验证）。
引用这句话时先确认场景，别当成普适规律。

### ⚠️ 别扫整页文案判「登录墙」

踩过一次：`_ssr_blank_reason` 里加过一版扫 `登录/注册`、`请先登录` 的判据，
结果**页头本来就有「登录/注册」入口**，任何空页都命中。后果是
`PlatformRiskStop` → `AccessGate.pause()` → **持久熔断** → 必须人工解冻，
一轮扫描在 8 个请求处就停了，而账号完全正常。

登录判定只能有一个权威来源：`_inline_is_login()` 读页面内联的
`_userInfo.isLogin` —— 结构化硬信号，零额外请求。任何「扫文案猜登录态」的第二套
判据都是净负担。

### 两个数字的适用范围（别照搬）

| 数字 | 出处 | 适用条件 |
|---|---|---|
| 约 **86 请求/IP** | `boss-scrape-layer` 记忆 | **刷新链路**走代理出口、2–4 秒节奏。采集链路是直连家庭 IP，不适用 |
| **10–16 秒自愈** | `boss_scrape/refresh.py:292` | **屏内窗口**、n=10~18。原文明确写「屏外窗口一直不跳转」—— 我们的 `window_position: 2400,1400` 是屏外，**不成立** |

### 阶梯（历史观测，方向可信、阈值不可套用）

```
正常 → JS 挑战（security.html，偶发） → 滑块（要人） → 403.html?code=31（24 小时）
```

**撞墙后不许继续请求** —— 每一级都不可逆地推高下一级。

### code 语义

| code | 含义 | 处置 |
|---|---|---|
| `37` | 环境存在异常（待人工判断） | 停，自动重算已停用 |
| `36` | 要求人工过验证 | 停，等人过滑块 |
| `31/32/35` | 封禁 | 停，等 `expireTime` |
| `7` | 登录失效 | 重新登录 |

`__zp_stoken__` 窗口**按请求次数算**（约 5 个），干等 30/90 秒都不恢复。

---

## 5. 数据判读：哪些字段会骗你

| 字段 | 陷阱 |
|---|---|
| **列表命中** | 死岗位也挂在列表里。`last_seen_at` 更新只证明被搜到过，**不证明在招** |
| **`seen_count`** | 是**次数**不是天数。同一天多关键词扫描会把它刷高，别当"挂了多少天" |
| **在招判定** | 用 `analysis/market.py::hiring_of()`：平台状态 + HR 活跃度 + 存在时长，五档 |
| **`company_reg_json`** | 默认值是 `'{}'`（长度 2）。用 `<> ''` 判断会把空对象误算成"有值"—— 实测真实覆盖只有 **727/11,818（6.2%）** |
| **参保人数** | 是**年报**字段：自报、可缺、滞后 6–18 个月，成立不足一年的公司没有 |
| **匿名公司名** | `某大型计算机软件公司` 这类通常是外包/代招，公司名被隐藏 |
| **推荐流** | 经验门槛偏松（会推 5–10 年的资深岗），画像命中率实测只有 **13/43（30%）**，且其中大半是外包 |

### 在招五档

`open`（HR 在线/刚刚活跃）→ `warm`（3–7 天）→ `cold`（8–30 天）→ `stale`（>30 天）
→ `closed`（页面明写关闭）。取**更乐观**的那个信号：`recruiter_online` 或文案带「在线」
直接判 `open`，因为实测有「HR 在线但 `active_days` 还停在 3 天前」的行。

---

## 6. 外包识别：三路信号并联

**JD 正文里几乎不写外包** —— 2026-10-19 实测 3,072 条在招且有正文的岗位里，
明写「劳务派遣 / 用工性质 / 与人力公司签约」的只有 **1 条**，`外派至` **0 条**。
不写的东西任何文本方法都找不到，LLM 也一样。

**往 JD 加关键词正则更糟**：`外包|外派|驻场|客户现场` 命中 124 条，其中 17 条是在
主动声明自己**不是**外包（「非外包项目！」「自研产品…非外包性质」），其余多为
实施/交付/售前/售后岗 —— `客户现场`、`驻场` 在中文技术 JD 里绝大多数指**工作地点**
而非用工主体。精确率约 1–2%。

所以识别只能靠**公司主体身份**，三路并联：

```python
staffing_signal(row) = 名单命中 | industry == '人力资源服务' | is_proxy_job
```

- **名单** `src/job_radar/data/staffing_companies.txt`（300 条主体名）抓得到 ITO 大厂
- **行业标签**抓得到名单没收的小微/区域人力公司
- **`is_proxy_job`** 是平台自己标的代招

2026-10-19 实测三路并集 **944 条 / 在招 236 条**，单看任何一路都少一截。

### 名单匹配规则（写死在 `profile.is_staffing_company()`）

- **强词**（≥4 汉字 或 ≥6 拉丁）→ 允许子串
- **短词**（<4 汉字 且 <6 拉丁）→ 只允许 ①全等 ②本身以前缀开头 ③去掉 2~4 字省市前缀后以前缀开头。**禁止子串**

短词禁止子串是因为实测踩过六个坑：`智联`（华秋智联/厦门鹭港智联算力）、`宇信`（海宇信息）、
`东软`（山东软件）、`微创`（上海微创医疗）、`海隆`（海隆石油）、`宝信`（宝信汽车）。
`tests/analysis/test_staffing.py` 把这些坑全锁住了。

---

## 7. 岗位判定：LLM 标签做主，不用正则

### 分工原则

| 层 | 谁做 | 产出 |
|---|---|---|
| **分类**（这是什么岗位） | 决策模型 `job_analysis.relabel` | `role_family` / `seniority_score` / `fresh_grad` / `grad_year` / `education_req` / `employment_type` / `is_proxy` |
| **抽取**（文中提了什么技术） | 规则 `job_analysis.tech` | `tech_stack_json`（词表匹配，确定性、零模型成本） |
| **策略**（对我合不合适） | `analysis/decisions.py::evaluate` | 分桶 + 排除理由 |

**为什么不用正则做分类**：每加一条正则都是给上一个漏判打补丁 —— 先加「正文要
硕博」，再加「标注岗要区分技术活和苦力」，再加「年限是期望值不是门槛」……
正则的根本问题是**它只能覆盖你想得到的情况**，想不到的全漏，而且每补一条
都可能像 `OD` 吃掉 `Coding` 那样误伤。

**为什么技术栈可以用规则**：技术词是**封闭集合**（Python / Redis / LangChain
写法固定），而「这个岗位到底算什么方向」是判断，不是匹配。分工要清楚。

### 打标只跑新数据

```powershell
python -m job_analysis.relabel                    # 只打近 7 天活跃的（默认）
python -m job_analysis.relabel --stats-only       # 只看新旧分布，不调模型
python -m job_analysis.relabel --max-age-days 0   # 不限时效，动存量（费额度）
```

判据用 `last_seen_at` 而不是 `first_seen_at`：三个月前首见、但今天又出现在
搜索结果里的岗位仍然是活的，值得打标。

⚠️ **加新问题会造成版本断层。** `education_req` 是 2026-10-19 新加的，
只有按 7 天窗口重标过的那批有值（318/7,502 = 4.2%），更早的 7,184 条是旧
schema 打的、这一列为空。所以**老的硕博岗不会被它拦住**。要补齐只有全量重标
一条路（约 78 分钟模型调用），按额度策略需显式决定。

同理，以后再加 schema 问题时要么接受断层，要么一次性全量重标。

### 判定规则（都在 `evaluate()`，每条都标了实测来源）

| 维度 | 硬排除 | 降级为「需核实门槛」 |
|---|---|---|
| **岗位族** | `role_family` 契合度 < 2，或 `ai_app`/`backend`/`fullstack`/`algorithm` 但技术栈无 AI 信号 | — |
| **经验** | 2 年以上（LLM `seniority_score` ≥ 1.6 或列表 ≥ 2 年） | — |
| **学历** | 正文要求硕/博（`education_req`） | — |
| **薪资** | 起薪 > 期望上限；或中点 > 25K | 顶薪 > 期望上限 ×2 |
| **应届** | 仅 `fresh_grad == "reject"` | — |
| **届别** | 仅 `grad_year` 为更晚届别 | — |
| **外包** | 名单 / 行业标签 / `is_proxy` ≥ 0.5 | — |
| **公司** | 注册资本 < 100 万 | 非公司主体 / 成立不足 12 月 |

### 三个踩过的判断错误

1. **`unstated` 不等于拒绝**。绝大多数 JD 根本不提应届，按「没提就是不要」判
   会误杀一大片。只有 `fresh_grad == "reject"` 才硬排除。
2. **列表 `education` 是搜索条件回显**，不是岗位门槛。实测某岗位列表标「本科」
   （因为搜索按本科筛），正文写「2026.9-2027.8 期间已获得**博士/硕士**学位的同学」。
   所以加了 `education` 这个独立问题（schema 第 8 个）。
3. **`role_family` 会把「AI数据标注专家」判成 `ai_app`** —— JD 里全是 LLM、
   大模型、训练这些词，模型读的是**工作对象**而不是**工作性质**。用技术栈一卡
   就分开了：标注岗是 `Python, SQL`，真 AI 开发岗有 `Agent, RAG, LangChain`。

### 薪资为什么看三个角度

只看「区间有没有交集」不够：`25-50K` 和期望的 `12-28K` 确实在 25-28 擦边，
但那说明招的是能独当一面的人。三个角度各自回答一个问题：

- **起薪 `min_k`** —— 岗位底线给到多少 → 超过期望上限就是不看简历
- **中点 `mid`** —— 典型水平 → 超过 25K 是资深岗
- **顶薪 `max_k`** —— 天花板 → 远超期望上限说明区间是给资深人开的

阈值来自库内实测（4,177 条在招）：应届友好岗中点 p50=12.5 / **p90=22.5**，
5-10 年档 p50=27.5，**25 是分界线**。

---

## 8. 关键词：怎么挖，怎么合并

### ⚠️ 选词的第一原则：精确词优先，泛化词删掉

2026-10-19 一轮 143 条新岗位只出 4 条可投。按关键词拆开看产出率：

| 关键词 | 带入 | 可投 | 可投率 |
|---|---:|---:|---:|
| 全栈 | 30 | 2 | 7% |
| **后端开发** | 27 | **0** | 0% |
| **LLM** | 26 | **0** | 0% |
| 大模型 | 23 | 1 | 4% |
| **推荐流** | 22 | **0** | 0% |
| RAG | 8 | 1 | 12% |

`后端开发` 带进来的 27 条全是 Java/Go/.NET 资深岗，`全栈` 带进来的是泛全栈。
**而全库在招量最高的几个精确词，那轮几乎没跑到**：智能体开发 563、
AI后端工程师 503、RAG 463、AI Agent 408。

所以：`全栈`、`后端开发` 直接从关键词表删掉。要覆盖全栈方向用
`AI全栈工程师` 这种**带 AI 限定**的写法。

### 顺序不是优先级

`rotate_keywords()` 按「最久没采过」轮转（`rounds.py`），保证每个词都会被轮到 ——
这是修 `config.keywords[:3]` 那个 bug 时加的（那版每轮都打同一批三个词，
实测连跑四轮插入数 154→217→49→31）。配置顺序只在首次运行当 tiebreak。

18 个词 / 每轮 3 个 = **6 轮覆盖一遍**。

### 挖词的唯一有效来源是**标题措辞**

正文里的技术词区分度低（几乎所有 AI 岗都写 Python/RAG），而标题是 HR 自己写的
招聘口径，直接决定平台怎么索引。

### 两个来源，用途不同

| 来源 | 覆盖 | 用途 |
|---|---|---|
| 全库标题（`derive_keywords.py`） | 11,818 条，按覆盖公司数排序 | 找**高产出**词 |
| 推荐流校准样本（`analyze_calibration.py`） | 40 条，**先按画像过滤** | 找**画像内**的真实措辞 |

推荐流的经验门槛偏松（43 条里 30% 画像内），所以**必须先按画像过滤再挖词**，
否则会把资深岗的词当信号。

⚠️ **高产 ≠ 该收**。`算法` 全库在招 368，但画像 roles 是 Agent/智能体/LLM/
大模型/RAG/AI应用，不含算法，且 `excluded_titles` 已排视觉算法/图像算法/机器视觉。

### 合并规则

现有词 + 校准新词 + 高产出词，去重后一起跑。2026-10-19 的合并结果（13 个）：

```
AI Agent / 智能体开发 / 大模型应用开发 / AI后端工程师 / RAG / Python
+ AI应用开发 / Agent开发 / 大模型 / 后端开发 / 全栈 / AI软件开发 / LLM
```

**`算法` 刻意不收**：画像 roles 是 Agent/智能体/LLM/大模型/RAG/AI应用，不含算法，
且 `excluded_titles` 已排 视觉算法/图像算法/机器视觉。高产 ≠ 该收。

### 持续优化

每轮跑完看 `keyword_progress` 表和新增岗位数，产出低的词降频、新挖到的词补进去。
`rounds.rotate_keywords()` 按最近使用时间轮转，避免每次都打同一批。

---

## 9. 常见坑清单

1. **裸子串做黑名单** —— `OD` 裸子串命中 91 条，绝大多数是 `Coding`；词边界才 20 条真 OD。
   英文缩写越短越危险。
2. **`<> ''` 判空** —— JSON 列默认值是 `'{}'` 不是 `''`。
3. **列表扫描"复活"旧岗位** —— 死岗位也挂在列表里。
4. **翻页与详情交错** —— 详情导航会离开列表页，下一页滚不动。
5. **pacing 没接线** —— `boss.py` 里所有节奏点都是 `getattr(self, "pace", None)`，
   拿不到就**什么都不做**。脚本路径必须自己接 `RequestPacer`，否则列表请求零间隔连发。
6. **SSR 详情被记成 `list`** —— `_kind_of()` 原先只认 `/wapi/zpgeek/job/detail.json`，
   `/job_detail/*.html` 落到 `list`，让"详情占多少请求"这类分析失效。
7. **`observed=False`** —— 补详情不是"又出现在列表里"，不能刷 `seen_count` / `last_seen_at`。
8. **`_legacy` 测试不在门禁里** —— 默认门禁只有 `tests/e2e`（`pyproject.toml`
   的 `testpaths`）。纯逻辑测试在 `tests/scrape` / `tests/analysis`，要显式跑。

---

## 10. 常用命令

```powershell
cd D:\Code\Boss
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING='utf-8'

# 校准（推荐流 5 页 + 40 条正文）
& .\.venv\Scripts\python.exe -X utf8 -m job_radar.cli session --phase calibrate --pages 5 --detail-limit 40

# 关键词扫描
& .\.venv\Scripts\python.exe -X utf8 -m job_radar.cli session --phase sweep --pages 1 --detail-limit 8 --keywords "..."

# 补存量正文
& .\.venv\Scripts\python.exe -X utf8 -m job_radar.cli session --phase backfill --detail-limit 60

# 先看解析对不对（0 请求）
& .\.venv\Scripts\python.exe -X utf8 -m job_radar.cli session --phase sweep --dry-run --keywords "..."

# 速率模拟（不联网、不 sleep，按真实 JD 字数算内容停留）
& .\.venv\Scripts\python.exe -X utf8 tools\simulate_pacing.py

# 死代码扫描（只读，零外部引用 / 仅测试引用 / 正常）
& .\.venv\Scripts\python.exe -X utf8 tools\deadcode_scan.py

# 打标（默认只跑近 7 天活跃的新数据）
& .\.venv\Scripts\python.exe -X utf8 -m job_analysis.relabel --stats-only
& .\.venv\Scripts\python.exe -X utf8 -m job_analysis.relabel

# 只补技术栈（纯规则，零模型成本）
& .\.venv\Scripts\python.exe -X utf8 -m job_analysis.tech

# 门禁：默认只有端到端（约 22 秒）
& .\.venv\Scripts\python.exe -X utf8 -m pytest -q

# 纯逻辑门禁（按需，不联网）：节奏 / 归因 / 外包名单 / 决策模型
& .\.venv\Scripts\python.exe -X utf8 -m pytest tests/scrape tests/analysis -q

# 历史行为查证（红着不代表现在有问题）
& .\.venv\Scripts\python.exe -X utf8 -m pytest tests/_legacy -q
```

风控熔断后：先 `job-radar risk-status` 看状态，人工处理完再 `job-radar risk-release
--confirm-account-recovered`。**不要靠等**。
