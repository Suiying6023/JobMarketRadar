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
| `boss-risk-control.md` | 访问开关、持久暂停、**读页面原文判风控类别**、三类停机分开提示 |
| `boss-refresh-chain.md` | 状态刷新：判定只降不升、冷却期、落库口径 |
| `boss-company-data.md` | 企业信息四层来源（**详情页自带 6 项工商、零请求** / 公司页 / 企查查 / 官方库） |
| `boss-data-and-filtering.md` | 数据所有权、主键、时间与薪资口径、**判定三层判据** |
| `boss-job-radar.md` | 通用数据服务的边界与形态 |
| `boss-keywords-and-tagging.md` | **决策模型 8 个标签 vs 规则抽技术栈**、只跑 7 天内新数据 |

## 和 `docs/` 其他文档的分工

- `docs/PLAYBOOK.md` —— **怎么跑、怎么判、哪里会骗你**（操作经验，带实测条件）
  第 2 节速率 / 第 3 节详情与工商 / 第 4 节风控 / **第 7 节岗位判定**
- `docs/ARCHITECTURE.md` / `DATA_MODEL.md` / `SERVICE_API.md` —— 代码结构与接口
  （`DATA_MODEL.md` 有「决策模型标签列」和「工商登记列」两张表）
- `docs/memory/` —— 本目录：按主题聚合的项目知识，偏「为什么这么设计」
