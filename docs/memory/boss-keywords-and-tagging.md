---
name: boss-keywords-and-tagging
description: BOSS 关键词与打标入口：统一词表、LLM配置与批次生效、输入/提示词版本、旧标签与来源证据分层
modified: 2026-09-13
volatile: true
metadata:
  type: project
---
# BOSS 关键词与标注

网页关键词页统一搜索词、目标角色、技能和排除词，使用搜索、分页与批量导入。配置与关键词页共用草稿和修订号。
覆盖统计按已抓岗位计算，英文大小写归一；独有覆盖表示只命中一个已观测关键词的记录数，多词覆盖分别解释。

LLM 设置在配置页，任务入口为 `job-radar task start tag`。本机 key 优先，api_key_env 为环境变量回退；
保存时空 key 保留原值，清除为独立操作，响应和配置历史隐藏密钥。打标将选中的岗位字段发往所配置端点。

目标集合在任务开始时确定；端点、模型与 prompt 的更新用于后续批次，已经完成或跳过的岗位通过新任务重标。
任务保存所用输入和提示词、来源哈希、模型、端点与输出，版本表见 [[boss-data-and-filtering]]。
原文或配置变化后，旧结果在页面标为待更新；模型结论与原始证据分别保留。

glm 是旧列名。旧 keep/demote、模型解释、薪资带和企业判断是历史意见，结合原文与任务语境使用。
词表表示检索和技术目标，不能由词命中推定用户已掌握。

相关：[[boss-job-radar]] [[boss-data-and-filtering]] [[job-hunt-workspace]]
