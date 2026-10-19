"""岗位分析层：用决策模型从 JD 正文重建标签。

与抓取层（`boss_scrape`）和存储层解耦——输入是一行岗位数据，输出是一组判定。

## 为什么用决策模型而不是对话模型

BOSS 的标题经常和正文不符（本地 7,417 条实测，可比对的 3,234 条里 **35.1%** 不一致）。
要解决的是"看完正文后重新判定岗位方向"，本质是**分类 + 是非 + 评分**，
不是生成文本。决策模型一次前向同时出这三样，还带概率分布和置信度：

  · 延迟 2.3 秒/条，输入 785 token，**成本与输出长度无关**
  · 返回 `choice` 的选项、`noul` 的 P(yes)、`score` 的期望值 + 各级概率

用对话模型做同一件事，既要它输出 JSON 又要防格式错，还更慢更贵。
"""

from .decision import DecisionClient, DecisionError, run_batch
from .schema import QUESTION_ROLE, QUESTIONS, build_state

__all__ = ["DecisionClient", "DecisionError", "run_batch",
           "QUESTIONS", "QUESTION_ROLE", "build_state"]
