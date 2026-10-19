---
name: boss-keywords-and-tagging
description: BOSS 打标两条路径与分工：决策模型出 8 个结构化标签（含学历/届别/资历）、规则抽技术栈、只跑 7 天内新数据
modified: 2026-10-19
volatile: true
metadata:
  type: project
---
# BOSS 关键词与标注

## 两条打标路径，别混

| 路径 | 入口 | 产出 | 用在哪 |
|---|---|---|---|
| **决策模型**（主） | `python -m job_analysis.relabel` | 8 个结构化列：`role_family` / `role_confidence` / `seniority_score` / `employment_type` / `fresh_grad` / `grad_year` / `education_req` / `is_proxy` | 岗位判定（`analysis/decisions.py`） |
| 旧对话模型 | `job-radar task start tag` → `job_radar/llm.py` | `glm_tags_json`（`keep`/`demote` + 自然语言理由） | 仅历史证据，**不再用于判定** |

`glm_tags_json` 是旧列的模型意见，`keep` 不等于可达性结论。它的提取模型、提示词
口径和时间标记都与决策模型不同，本版不覆写这批证据。

## 决策模型：8 个问题，全是「岗位的客观属性」

| 问题 | 类型 | 选项 |
|---|---|---|
| `role` | choice | 19 个岗位族（`ai_app` / `algorithm` / `backend` / `other` …） |
| `mismatch` | noul | 标题方向词与正文职责是否不一致 |
| `seniority` | score | 0–4 连续值 |
| `employment` | choice | fulltime / internship / parttime / campus |
| `fresh_grad` | choice | accept / maybe / reject / unstated |
| `grad_year` | choice | 2026 / 2027 / other_year / any_year / unstated |
| `is_proxy` | noul | 外包 / 劳务派遣 / 中介代招概率 |
| **`education`** | choice | phd / master / bachelor / college / any / unstated |

**这个分层是刻意的**：模型只回答「这个岗位是什么」，不回答「对我合不合适」。
合不合适由 `evaluate()` 按画像映射 —— 换画像时不必重标。

⚠️ **`education` 是 2026-10-19 新加的**。列表的 `education` 字段是**搜索条件的
回显**，不是岗位门槛：实测某岗位列表标「本科」（因为搜索时按本科筛），正文写的
却是「2026.9-2027.8 期间已获得或即将获得**博士/硕士**学位的同学」。

## 技术栈走规则，不走模型

`job_analysis/tech.py` 用词表匹配抽 `tech_stack_json`，零模型成本。

**为什么**：技术词是**封闭集合**（Python / Redis / LangChain 写法固定），
而「这个岗位算什么方向」是判断。分工要清楚。

它的实际用途不止「列技术」—— 还纠正了 `role_family` 的一类误判：
决策模型会把「AI数据标注专家」判成 `ai_app`（JD 里全是 LLM、大模型、训练这些词，
模型读的是**工作对象**而不是**工作性质**），但技术栈只有 `Python, SQL`；
真 AI 开发岗有 `Agent, RAG, LangChain`。一卡就分开。

## 只跑新数据

```powershell
python -m job_analysis.relabel                    # 只打近 7 天活跃的（默认）
python -m job_analysis.relabel --stats-only       # 只看新旧分布，不调模型
python -m job_analysis.relabel --max-age-days 0   # 不限时效，动存量（费额度）
```

判据用 `last_seen_at` 而不是 `first_seen_at`：三个月前首见、但今天又出现在搜索
结果里的岗位仍然是活的，值得打标。

实测速率：并发 14，约 **1.6 条/秒**（100 条/分）。全库 7,502 条约 78 分钟。

## 关键词

词表表示检索和技术目标，**不能由词命中推定用户已掌握**。
挖词来源与合并规则见 `docs/PLAYBOOK.md` 第 8 节。

相关：[[boss-job-radar]] [[boss-data-and-filtering]] [[boss-company-data]]
