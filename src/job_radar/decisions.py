"""Native Jev decision calls for bounded classifications and research labels."""
from __future__ import annotations

import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from .llm import LLMError, SameOriginRedirect


def ask_decisions(base_url, model, api_key, state, questions, *, timeout=40, proxy_url=""):
    """Return validated typed answers and observed model/usage/latency metadata."""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/systemone"):
        endpoint += "/systemone"
    payload = {"model": model, "state": state, "questions": questions}
    request = Request(endpoint, method="POST", data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": "Bearer " + api_key})
    start = time.perf_counter()
    try:
        proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else {}
        with build_opener(ProxyHandler(proxies), SameOriginRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise LLMError("Jev响应超过2MB")
        result = json.loads(raw)
    except HTTPError as error:
        raise LLMError(f"Jev端点返回HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        raise LLMError(f"Jev连接失败（{type(reason).__name__}）") from None
    except ValueError:
        raise LLMError("Jev返回了无效JSON") from None
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise LLMError("Jev答案与问题集合不匹配")
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise LLMError("Jev答案类型不匹配")
        def probability(value):
            return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
        if question["type"] == "noul":
            if not probability(answer.get("noul")):
                raise LLMError("Jev概率无效")
        elif question["type"] == "choice":
            criteria = question["criteria"]
            probabilities = answer.get("probabilities")
            if (answer.get("choice") not in criteria or not probability(answer.get("confidence"))
                    or not isinstance(probabilities, dict) or set(probabilities) != set(criteria)
                    or not all(probability(v) for v in probabilities.values())
                    or abs(sum(probabilities.values()) - 1) > 0.02):
                raise LLMError("Jev选项或概率分布无效")
        else:
            raise LLMError("当前岗位决策只配置choice与noul")
    if not isinstance(result.get("model"), str) or not result["model"]:
        raise LLMError("Jev缺少实际模型版本")
    metadata = {"requested_model": model, "resolved_model": result["model"], "endpoint": endpoint,
                "elapsed_seconds": round(time.perf_counter()-start, 4), "usage": result.get("usage", {})}
    return answers, metadata


def job_questions():
    questions = {
        "role_family": {"type": "choice", "instructions": "仅依据岗位的主要职责选岗位族，普通使用AI编程工具不等于专门开发AI编程工具。",
            "criteria": {
                "agent_application": "开发Agent/LLM/RAG应用及其后端",
                "ai_coding": "主要负责AI辅助研发工作流、编码Agent或AI代码工程化",
                "ai_delivery_fde": "主要负责客户现场AI方案、系统集成、部署、交付或实施",
                "ai_data_engineering": "主要负责LLM/AI的数据管线与数据质量",
                "ai_evaluation": "主要负责AI模型或Agent评测与质量",
                "model_algorithm": "主要负责模型训练、算法研究、推理算法",
                "software_backend": "主要是常规后端开发",
                "frontend_fullstack": "主要是前端或通用全栈开发",
                "other": "其他职责或无法判断"}},
        "graduate_statement": {"type": "choice", "instructions": "仅按正文对应届生的明确表态分类；年限要求和毕业届别另判断，不把年限要求改写为明确拒绝应届。",
            "criteria": {"unspecified": "未明确表态", "accepted": "明确接受应届或无经验毕业生",
                         "conditional": "优秀应届或项目优秀等条件可放宽", "rejected": "明确写不接受/不考虑应届生"}},
        "school_requirement": {"type": "choice", "instructions": "区分院校硬门槛、优先和未说明。普通学历要求不是院校排名要求。",
            "criteria": {"unspecified": "未提院校层次", "preferred": "985/211/名校等仅为优先",
                         "required": "明确要求985/211/特定层次院校", "unrestricted": "明确表示院校不限"}},
        "requires_later_graduation": {"type": "noul", "instructions": "正文是否有只接受2027届及以后或在读生的限制，且未给国内2026届毕业生同等入口？"},
        "customer_delivery": {"type": "noul", "instructions": "主要职责是否包含对客户业务需求调研、方案集成、部署验收或交付，而非仅内部研发？"},
        "production_reliability": {"type": "noul", "instructions": "职责或要求是否明确涉及上线后的可靠性、错误恢复、可观测性、性能、成本或权限治理？"},
    }
    technologies = {
        "python": "Python", "sql": "SQL或关系数据库", "api": "HTTP/API/接口服务", "docker": "Docker或容器化",
        "rag": "RAG或检索增强生成", "vector_retrieval": "Embedding、向量检索或向量数据库", "rerank": "Rerank、重排或重排序",
        "tool_calling": "Function Calling、Tool Calling/Use、函数或工具调用", "workflow": "工作流或Workflow",
        "langchain": "LangChain", "langgraph": "LangGraph", "mcp": "MCP协议或MCP Server/Client",
        "multi_agent": "多Agent或多智能体协作", "context": "上下文工程、上下文管理、压缩或上下文窗口",
        "memory": "Agent记忆、长期记忆、会话记忆或Memory", "harness": "Agent Harness或Eval Harness",
        "eval": "AI/Agent/模型评测、Benchmark或Badcase", "ai_coding": "AI编程工具、AI辅助开发或Vibe Coding",
        "jev": "Jev或System One决策模型", "fde": "FDE或Forward Deployed Engineer或前线部署工程师",
        "fine_tuning": "微调、LoRA或SFT", "nl2sql": "NL2SQL、Text-to-SQL或自然语言查询数据库",
    }
    for name, description in technologies.items():
        questions["tech_" + name] = {"type": "noul", "instructions": f"岗位原文是否明确提及{description}？仅根据原文，不补充该类岗位通常会用的技术。"}
    return questions
