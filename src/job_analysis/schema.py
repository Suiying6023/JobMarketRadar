"""决策问题定义与状态构造。

问题数量直接影响延迟（官方文档：延迟随问题数近线性增长），所以只留
**分析真正用得上**的六项，且每项都能落成一列：

| 问题 | 类型 | 落库字段 | 用途 |
|---|---|---|---|
| role | choice | role_family / role_confidence | 用正文判定方向，解决标题误导 |
| mismatch | noul | title_mismatch | 标题与正文是否不符 |
| seniority | score | seniority_score | 资历档位（可落在两级之间） |
| employment | choice | employment_type | 全职/实习/兼职/校招 |
| fresh_grad | choice | fresh_grad | 是否接受应届（2026 届最关心） |
| is_proxy | noul | is_proxy | 外包/派遣/人力中介 |

`role` 的 instructions 里**明确要求忽略标题**——这是整个设计的目的。
"""

from __future__ import annotations

ROLE_OPTIONS: dict[str, str] = {
    # ⚠️ 必须有 `ai_app` 这一档。第一版没有它，结果 74% 的岗位被判成 `other` ——
    # 因为本地语料里大量是「AI Agent 开发 / 大模型应用」这类岗位，它们既不是
    # 传统后端、也不是模型训练，模型无处可放只能塞 `other`。分类法缺一档，
    # 模型不会告诉你，它只会默默把东西丢进兜底项。
    "ai_app": "AI 应用开发、智能体/Agent 开发、LLM 应用工程、提示词与 RAG 工程",
    "algorithm": "算法、模型训练、微调、推理优化、机器学习工程",
    # ⚠️ backend 必须包含"通用应用开发"。第二版把它窄化成"后端服务/接口/分布式"，
    # 结果「Python」「python开发工程师」「数据库工程师」这类岗位全被判成 other
    # （实测 other 里前几名正是这些标题）。它们不是后端架构，但**明确是写应用代码**，
    # 归 backend 比归 other 有用得多——other 的用途是标记"这不是技术岗"。
    "backend": ("后端服务、接口、分布式系统，以及通用应用/业务系统开发"
                "（用 Python/Java/Go/C++ 等写服务、脚本、工具、爬虫），"
                "只要正文描述的是编写应用代码而无法归入更具体的类别"),
    "frontend": "前端、客户端、页面与交互实现",
    "fullstack": "前后端都要做，且正文明确两边都涉及",
    "data": "数据仓库、数据分析、BI、数据治理、数据库开发与运维",
    "test": "测试、质量保障、自动化测试",
    "ops": "运维、SRE、基础设施、DevOps",
    "product": "产品经理、需求分析、产品规划",
    "operation": "运营、增长、内容、社群",
    "sales": "销售、商务、客户拓展",
    "design": "UI/UX、视觉、交互设计",
    "management": "项目经理、交付管理、团队管理",
    # ↓ 以下五档是从 `other` 里拆出来的。实测 `other` 的 top 标题是
    #   rpa开发工程师 / 亚马逊产品开发 / AIGC设计师 / AIGC导演 / AIGC讲师 /
    #   FPGA开发 / 实施工程师 —— 它们彼此完全不是一回事，混在一档里没法用。
    "embedded": "嵌入式、FPGA、单片机、驱动、固件、底层硬件编程",
    "content": ("AIGC 内容制作：视频/动画/短剧/编剧/导演/抽卡/视觉设计等"
                "以产出内容作品为主的岗位"),
    "teaching": "讲师、培训、教研、课程研发",
    "ecommerce": "电商产品开发、选品、采购、供应链、供应商管理",
    "implementation": "实施工程师、交付、驻场部署、客户现场技术支持",
    "other": ("以上都不是。**软件类岗位绝对不要选它**。可执行判据："
              "只要正文里出现编程语言（Python/Java/Go/C++/C#/Rust/JavaScript 等）"
              "或出现「开发」「研发」「编码」「接口」「系统设计」这类职责词，"
              "就不是 other —— 无法归入更具体的类别时选 backend。"
              "other 只留给财务、行政、人事、法务、纯内容制作这类岗位"),
}

SENIORITY_LEVELS = [
    "实习或应届，无经验要求",
    "初级，1-3 年",
    "中级，3-5 年",
    "高级，5-8 年",
    "专家或管理，8 年以上",
]

EMPLOYMENT_OPTIONS: dict[str, str] = {
    "fulltime": "全职正式岗位",
    "internship": "实习岗（在校生或日薪）",
    "parttime": "兼职、日结、非全日制",
    "campus": "校招、应届生专项通道",
}

FRESH_GRAD_OPTIONS: dict[str, str] = {
    "accept": "正文明确表示接受应届生或毕业生",
    "maybe": "正文暗示应届可放宽，但没有明确说接受",
    "reject": "正文明确表示不接受应届生",
    "unstated": "正文没有提到应届相关要求",
}

# ⚠️ 届别必须单独问。
#
# 用户是 2026 届。BOSS 上很多岗位写「2027 届」——那是**下一届**，2026 届投不了
# （虽然有的不强行限制）。把"接受应届"当成一个布尔值，会把 2027 届的岗位
# 也算成"可投"，这是方向性错误。
GRAD_YEAR_OPTIONS: dict[str, str] = {
    "2026": "正文明确面向 2026 届毕业生（或 2026 年毕业）",
    "2027": "正文明确面向 2027 届毕业生（或 2027 年毕业）",
    "other_year": "正文限定了其它具体毕业年份（2025 届、2028 届等）",
    "any_year": "正文说接受应届/毕业生，但没有限定具体届别",
    "unstated": "正文没有提到毕业届别",
}

# ⚠️ 学历必须单独问，不能只看列表的 `education` 字段。
#
# 2026-10-19 实例：某岗位列表标「本科」（因为搜索时按本科筛选），正文却写
# 「2026.9-2027.8 期间已获得或即将获得**博士/硕士**学位的同学」。列表字段是
# 搜索条件的回显，不是这个岗位的真实门槛 —— 只看它会把硕博岗放进本科可投列表。
EDUCATION_OPTIONS: dict[str, str] = {
    "phd": "正文要求博士（或博士优先但不排除硕士以外）",
    "master": "正文要求硕士及以上，本科不符合",
    "bachelor": "正文要求本科及以上（本科即可）",
    "college": "正文要求大专及以上，本科满足",
    "any": "正文明确学历不限",
    "unstated": "正文没有提到学历要求",
}

QUESTIONS: dict[str, dict] = {
    "role": {
        "type": "choice",
        "instructions": (
            "只根据正文描述的**实际工作内容**判断这个岗位属于哪个方向。"
            "标题可能有误导（例如标题写算法但实际做测试），**不要被标题影响**。"),
        "criteria": ROLE_OPTIONS,
    },
    "mismatch": {
        "type": "noul",
        "instructions": (
            "比较两件事：①标题里的**方向词**所暗示的岗位类别"
            "（例如标题含「算法」暗示算法岗，含「测试」暗示测试岗，"
            "含「产品」暗示产品岗，含「运营」暗示运营岗）；"
            "②正文描述的**主要职责**属于哪一类工作。"
            "如果两者类别不同（如标题写算法但正文主要是写测试用例、"
            "标题写产品但正文是研发实现），回答是。"
            "如果标题只是泛泛的「AI 应用」「智能体」这类不指向具体类别的词，"
            "或者两者类别相同，回答否。"),
    },
    "seniority": {
        "type": "score",
        "instructions": "这个岗位要求的资历属于哪一档？依据正文里的年限与职责范围。",
        "criteria": SENIORITY_LEVELS,
    },
    "employment": {
        "type": "choice",
        "instructions": "这个岗位的用工性质是什么？注意日薪、每周到岗天数、在校生等线索。",
        "criteria": EMPLOYMENT_OPTIONS,
    },
    "fresh_grad": {
        "type": "choice",
        "instructions": "正文对是否接受应届毕业生的表态是哪一种？没有提到就选未说明。",
        "criteria": FRESH_GRAD_OPTIONS,
    },
    "grad_year": {
        "type": "choice",
        "instructions": (
            "正文或标题限定了哪一届毕业生？判断顺序如下，逐条对照：\n"
            "1. 出现明确届别就选那一届。「27届」「26届」这种省略写法也算明确，"
            "「2027 届」「2027 年毕业」同理；2025/2028 等其它年份选 other_year。\n"
            "2. 没有明确届别，但出现「应届」「毕业生」「校招」「应届生」字样，选不限年份。\n"
            "3. **两者都没有，必须选未说明**。不要因为岗位看起来像校招就选不限年份——"
            "只有文中真的写了应届相关字样才算。"),
        "criteria": GRAD_YEAR_OPTIONS,
    },
    "is_proxy": {
        "type": "noul",
        "instructions": (
            "这个岗位是否为外包、劳务派遣或人力中介代招？"
            "例如正文写「长期驻场客户现场」「外包岗位」「由某人力公司代招」。"),
    },
    "education": {
        "type": "choice",
        "instructions": (
            "正文要求的**最低学历**是哪一档？只看正文，不要看标题或外部信息。\n"
            "1. 写「博士」或「博士优先」→ phd。\n"
            "2. 写「硕士及以上」「硕士研究生」「硕士学历」→ master。"
            "注意「硕士优先」不算门槛，若同时说「本科及以上」则选 bachelor。\n"
            "3. 写「本科及以上」「统招本科」→ bachelor；没写学历但要求「本科专业」"
            "同理选 bachelor。\n"
            "4. 写「大专及以上」「专科」→ college。\n"
            "5. 写「学历不限」→ any。\n"
            "6. **正文完全没提学历 → unstated**。不要因为岗位看着像研发岗就默认本科。"),
        "criteria": EDUCATION_OPTIONS,
    },
}

QUESTION_ROLE = "role"

# 正文截断长度。官方上下文上限 65536 token，中文约 1 token/字，
# 6000 字足够覆盖绝大多数 JD，又能把单条成本压在 1k token 量级。
MAX_DESCRIPTION_CHARS = 6000


def build_state(row: dict, *, max_chars: int = MAX_DESCRIPTION_CHARS) -> dict:
    """把一行岗位数据转成模型要看的 `state`。

    刻意**带上标题**：判断"标题与正文是否不符"必须有标题。
    但 `role` 的 instructions 要求模型只看正文。
    """
    description = (row.get("job_description") or "").strip()
    return {
        "title": (row.get("title") or "").strip(),
        "company": (row.get("company_name") or "").strip(),
        "salary": (row.get("salary_text") or "").strip(),
        "experience": (row.get("experience") or "").strip(),
        "education": (row.get("education") or "").strip(),
        "description": description[:max_chars],
        "description_truncated": len(description) > max_chars,
    }


def _confidence(answer: dict) -> float | None:
    value = answer.get("confidence")
    return float(value) if isinstance(value, (int, float)) else None


def _probabilities(answer: dict) -> dict:
    value = answer.get("probabilities")
    return value if isinstance(value, dict) else {}


def flatten(answers: dict) -> dict:
    """把 `answers` 摊平成可直接落库的列。

    概率保留原样（JSON），判定结果单独成列——两者都要：
    分析时要能按置信度过滤，回溯时要能看到模型当时有多犹豫。
    """
    role = answers.get("role") or {}
    mismatch = answers.get("mismatch") or {}
    seniority = answers.get("seniority") or {}
    employment = answers.get("employment") or {}
    fresh = answers.get("fresh_grad") or {}
    grad_year = answers.get("grad_year") or {}
    proxy = answers.get("is_proxy") or {}
    education = answers.get("education") or {}

    mismatch_p = mismatch.get("noul")
    proxy_p = proxy.get("noul")
    return {
        "role_family": role.get("choice"),
        "role_confidence": _confidence(role),
        "role_probabilities": _probabilities(role),
        "title_mismatch": (float(mismatch_p)
                           if isinstance(mismatch_p, (int, float)) else None),
        "seniority_score": (float(seniority["score"])
                            if isinstance(seniority.get("score"), (int, float)) else None),
        "seniority_confidence": _confidence(seniority),
        "employment_type": employment.get("choice"),
        "employment_confidence": _confidence(employment),
        "fresh_grad": fresh.get("choice"),
        "fresh_grad_confidence": _confidence(fresh),
        "grad_year": grad_year.get("choice"),
        "grad_year_confidence": _confidence(grad_year),
        "is_proxy": (float(proxy_p) if isinstance(proxy_p, (int, float)) else None),
        "education_req": education.get("choice"),
        "education_confidence": _confidence(education),
    }
