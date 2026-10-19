# 项目知识库（从 `D:\Code\Memory` 迁入）

这些文件原在 `D:\Code\Memory\knowledge\30-projects\boss\`，2026-10-19 迁到本项目。
Memory 那边只保留索引指针。

**为什么迁**：这些内容与代码同版本演进 —— 抓取层的行为、风控阈值、数据口径、
筛选规则都写在 `src/` 里，文档放代码仓库才能和改动一起 review、一起提交。
分成两个仓库时，代码改了文档不动，副本就开始漂移。

**修改时改这里，不要改 Memory 里的指针。**

| 文件 | 内容 |
|---|---|
| `boss-scrape-layer.md` | 独立抓取层：stoken 滚动、SSR 详情、三种短页面、风控阶梯 |
| `boss-risk-control.md` | 访问开关、持久暂停、任务归属校验 |
| `boss-refresh-chain.md` | 状态刷新：判定只降不升、冷却期、落库口径 |
| `boss-company-data.md` | 企业信息三层来源（BOSS 公司页 / 企查查 MCP / 官方库） |
| `boss-data-and-filtering.md` | 数据所有权、主键、时间与薪资口径、评分分工 |
| `boss-job-radar.md` | 通用数据服务的边界与形态 |
| `boss-keywords-and-tagging.md` | 关键词表、LLM 配置、打标版本 |

## 和 `docs/` 其他文档的分工

- `docs/PLAYBOOK.md` —— **怎么跑、怎么判、哪里会骗你**（操作经验，带实测条件）
- `docs/ARCHITECTURE.md` / `DATA_MODEL.md` / `SERVICE_API.md` —— 代码结构与接口
- `docs/memory/` —— 本目录：按主题聚合的项目知识，偏「为什么这么设计」
