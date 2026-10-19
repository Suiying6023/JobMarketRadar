---
name: boss-company-data
description: BOSS 企业信息三层来源：BOSS 公司页免费拿 15 项工商照面、企查查 MCP 补参保人数与风险、官方四库基本被墙
modified: 2026-10-19
volatile: true
metadata:
  type: project
---

# BOSS 企业信息来源

## 三层，各管一段

| 层 | 来源 | 字段 | 成本 |
|---|---|---|---|
| 工商照面 | `BOSS /gongsi/{brandId}.html` 的 `.business-detail li` | 15 项（含经营范围、所属行业、所属地区） | **¥0**，92.6% 覆盖 |
| 精确工商 + 风险 | 企查查 MCP（`agent.qcc.com`） | 26 项（含参保人数、实缴资本、纳税人资质、国标行业四级） | 按积分 |
| 官方原始 | GSXT / 信用中国 / 执行信息公开网 | — | 基本不可用，见下 |

## BOSS 公司页：免费，92.6%

`.business-detail li` 的 label/value 对，**页面给什么收什么**（不是写死标签）：

```
企业名称 法定代表人 成立时间 企业类型 经营状态 注册资本 注册地址 营业期限
所属地区 统一社会信用代码 核准日期 曾用名 登记机关 所属行业 经营范围
```

覆盖：去重公司 6,722，有 `brand_id` 5,414，没有 `brand_id` 但 `company_url` 里有 `/gongsi/`
的再 +1,175，合计 **6,225（92.6%）**。按 5.6 秒/家，全量约 9 小时，活跃子集约 4.6 小时。

脚本 `local/analysis/live-20261019/enrich_company_info.py`。三个性质必须记住：

- **用独立的未登录 profile**（`.browser-profile-reg`），与主账号 cookie 完全隔离。
  实测主账号正在人工验证时，公司页照样跑通——风控看到的是匿名访客，不是被标记的账号。
- 页面出现 `请完成验证` / `滑动验证` / `访问过于频繁` / `存在异常行为` 任一字样 →
  **立即终止整轮**、保留页面、退出码 3，不重试。
- 这个源**拿不到**参保人数、行政处罚、经营异常、失信、司法、股权结构。

## 企查查 MCP：一次调用补上参保人数

`https://agent.qcc.com/mcp/{company,risk,ipr,operation,history,executive}/stream`，
Bearer 认证，152 个工具。`get_company_registration_info` 返回 26 字段，其中
`参保人数`、`人员规模`、`实缴资本`、`纳税人资质`、`国标行业{门类,大类,中类,小类}` 是 BOSS 没有的。

- `history` server 连得上但 0 个工具（需企业认证），个人账号 5 个有工具。
- `tools/list` 与 `resources/list` **不耗积分**，只有 `tools/call` 耗。
- 插件 `qcc-dsh-mcp-oauth`（`dsh plugin --profile web add`）装好后要重启 `dsh web`。
- 积分：1 元 = 10 积分，工具 1/3/5/20 积分每次，注册送 500、每日 100。
  **剩余积分从 MCP 侧读不到**，只能去控制台看，或用「跑 10 家前后各看一次」反推单价。

## 参保人数：它是年报字段，不是工商照面字段

来源是企业年报的「社保信息」——企业**自己申报**、**可以不公示**、是**上一年度**的数、
成立不满一年**没有年报**。所以：

- BOSS 公司页没有它（整页 2,054 字，`参保`/`社保` 零命中）
- 已富化的 686 行里 **13% 成立 ≥2025 年**，必然没有
- 实际命中率乐观估计一半

**判断「是不是空壳」不要只靠它**。已经有的替代信号覆盖 100%：
`company_size`（BOSS 自标 6 档）+ 在招岗位数 + 注册资本 + 成立时间。
交叉表里最有用的组合是「标 10000 人以上却只挂 1 个岗位」的 118 家——比参保人数更能说明岗位真伪。

## 官方四库：从本机基本被墙（2026-10-19 实测）

| 站 | 状态 | 说明 |
|---|---|---|
| `gsxt.gov.cn` | **521** | 加速乐 JS 挑战（`__jsluid` + 混淆 JS 算 `__jsl_clearance`）+ 极验滑块 |
| `creditchina.gov.cn` | **412** | WAF 前置 |
| 专利检索 / 商标局 | **412 / 403** | 反爬 / IP 被拒 |
| `zxgk.court.gov.cn` / 裁判文书网 / 版权局 | 200 | 通，但只有站内搜索，无结构化接口 |

省级子站（`gsxt.zj.gov.cn` 等）**TLS 层就断**。公共数据开放平台确有 API 型数据，但走浙里办实名、
受限数据要等保二级 + 场景审核，点查不可行。

**结论：要结构化数据只能买，抓不动。** 而且 2026-09 到 10 月间
`wgpsec/ENScan_GO`（4,608★，最好的爱企查封装）被删库了——这个方向已经不稳。

## 自建 enrichment 脚本的坑

`enrich_company_info.py` 的旧版 import 了已删除的 `job_radar.refresh_fetch`
（`launch_args` 已迁到 `collectors.browser`）。全仓 35 个归档文件踩同一个坑。
旧版还有两个缺陷：只认 `brand_id`（漏 1,175 家）、写死 11 个标签（抓不到经营范围）。

相关：[[boss-data-and-filtering]] [[boss-scrape-layer]] [[boss-refresh-chain]]
