"""技术栈抽取：从 JD 正文 + 平台技能词里抽结构化技术标签。

## 为什么用规则而不是模型

技术词是**封闭集合**：Python / Java / Kafka / Redis 这些写法固定，正则足够。
用模型去抽，慢、贵、还可能把"熟悉任意一门语言"抽成 "任意"。
规则抽取是确定性的——同样的输入永远同样的输出，可以审计。

模型的强项是**判断**（这个岗位到底算什么方向），不是**匹配**（文中提没提 Kafka）。
分工要清楚。

## 覆盖

平台自带 `skills` 字段只覆盖 67.3% 的岗位，而且常常是空的（实测 `skills: []`）。
从 JD 正文抽能把剩下的补上，同时给出**按类别聚合**的结果，便于做"这个岗位
到底要什么技术栈"的比较。

## 短词要小心

`Go`、`R`、`C` 这类短词用子串匹配会误伤（`Google`、`React`、`CI/CD`）。
所以：短词用**词边界**匹配，且维护一份排除词。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# 规范名 → (别名元组, 是否短词需边界匹配)
VOCAB: dict[str, tuple[tuple[str, ...], bool]] = {
    # 语言
    "Python": (("python",), False),
    "Java": (("java",), False),
    "Golang": (("golang", "go语言", "go 语言", "go开发"), False),
    "C++": (("c++", "cpp"), False),
    "C#": (("c#", "csharp", ".net"), False),
    "Rust": (("rust",), False),
    "JavaScript": (("javascript", "js开发"), False),
    "TypeScript": (("typescript",), False),
    "PHP": (("php",), False),
    "Scala": (("scala",), False),
    "Kotlin": (("kotlin",), False),
    "Swift": (("swift",), False),
    "Shell": (("shell", "bash"), False),
    # 框架 / 服务端
    "Spring": (("spring",), False),
    "Django": (("django",), False),
    "Flask": (("flask",), False),
    "FastAPI": (("fastapi",), False),
    "Gin": (("gin框架", "gin 框架"), False),
    "Node.js": (("node.js", "nodejs"), False),
    "gRPC": (("grpc",), False),
    "LangChain": (("langchain",), False),
    "LlamaIndex": (("llamaindex", "llama_index"), False),
    "Dify": (("dify",), False),
    "Coze": (("coze",), False),
    "n8n": (("n8n",), False),
    # 数据库
    "MySQL": (("mysql",), False),
    "PostgreSQL": (("postgresql", "postgres", "pg数据库"), False),
    "Redis": (("redis",), False),
    "MongoDB": (("mongodb", "mongo"), False),
    "Elasticsearch": (("elasticsearch", "es检索"), False),
    "ClickHouse": (("clickhouse",), False),
    "Oracle": (("oracle",), False),
    "SQLite": (("sqlite",), False),
    # 中间件 / 基础设施
    "Kafka": (("kafka",), False),
    "RabbitMQ": (("rabbitmq",), False),
    "Docker": (("docker",), False),
    "Kubernetes": (("kubernetes", "k8s"), False),
    "Nginx": (("nginx",), False),
    "Linux": (("linux",), False),
    "Celery": (("celery",), False),
    # 大模型 / AI
    "PyTorch": (("pytorch",), False),
    "TensorFlow": (("tensorflow",), False),
    "Transformers": (("transformers",), False),
    "vLLM": (("vllm",), False),
    "RAG": (("rag", "检索增强"), False),
    "Agent": (("agent", "智能体"), False),
    "Prompt": (("prompt", "提示词"), False),
    "Fine-tune": (("微调", "fine-tune", "finetune", "sft"), False),
    "向量数据库": (("向量数据库", "milvus", "faiss", "chroma", "pgvector", "qdrant"), False),
    "多模态": (("多模态", "multimodal"), False),
    "CUDA": (("cuda",), False),
    # 数据
    "Spark": (("spark",), False),
    "Flink": (("flink",), False),
    "Hadoop": (("hadoop",), False),
    "Airflow": (("airflow",), False),
    "SQL": (("sql",), False),
    "ETL": (("etl",), False),
    # 前端
    "React": (("react",), False),
    "Vue": (("vue",), False),
    "Next.js": (("next.js", "nextjs"), False),
    "Webpack": (("webpack",), False),
    # 其它
    "Git": (("git",), False),
    "CI/CD": (("ci/cd", "cicd", "持续集成"), False),
    "微服务": (("微服务", "microservice"), False),
    "高并发": (("高并发",), False),
    "分布式": (("分布式",), False),
}

# 短词（≤2 字符或极易撞车）单独走边界匹配，并排除常见误伤。
_SHORT_TERMS: dict[str, tuple[str, ...]] = {
    "Go": (r"\bgo\b", r"\bgo语言\b"),
    "R": (r"\br语言\b",),
    "C": (r"\bc语言\b",),
}
_SHORT_EXCLUDE = ("google", "django", "mongo", "cargo", "algorithm")

CATEGORY: dict[str, str] = {
    "Python": "语言", "Java": "语言", "Golang": "语言", "C++": "语言", "C#": "语言",
    "Rust": "语言", "JavaScript": "语言", "TypeScript": "语言", "PHP": "语言",
    "Scala": "语言", "Kotlin": "语言", "Swift": "语言", "Shell": "语言",
    "Spring": "框架", "Django": "框架", "Flask": "框架", "FastAPI": "框架",
    "Gin": "框架", "Node.js": "框架", "gRPC": "框架",
    "LangChain": "AI工程", "LlamaIndex": "AI工程", "Dify": "AI工程",
    "Coze": "AI工程", "n8n": "AI工程",
    "MySQL": "数据库", "PostgreSQL": "数据库", "Redis": "数据库",
    "MongoDB": "数据库", "Elasticsearch": "数据库", "ClickHouse": "数据库",
    "Oracle": "数据库", "SQLite": "数据库", "SQL": "数据库",
    "Kafka": "中间件", "RabbitMQ": "中间件", "Docker": "中间件",
    "Kubernetes": "中间件", "Nginx": "中间件", "Linux": "中间件", "Celery": "中间件",
    "PyTorch": "AI", "TensorFlow": "AI", "Transformers": "AI", "vLLM": "AI",
    "RAG": "AI", "Agent": "AI", "Prompt": "AI", "Fine-tune": "AI",
    "向量数据库": "AI", "多模态": "AI", "CUDA": "AI",
    "Spark": "数据", "Flink": "数据", "Hadoop": "数据", "Airflow": "数据", "ETL": "数据",
    "React": "前端", "Vue": "前端", "Next.js": "前端", "Webpack": "前端",
    "Git": "工具", "CI/CD": "工具",
    "微服务": "架构", "高并发": "架构", "分布式": "架构",
}


def extract(text: str, extra: list[str] | None = None) -> dict:
    """从正文（和平台技能词）里抽技术栈。返回 `{类别: [规范名]}` 与扁平列表。"""
    haystack = (text or "").lower()
    for item in extra or []:
        haystack += " " + str(item).lower()

    found: dict[str, set[str]] = {}
    for canonical, (aliases, _) in VOCAB.items():
        if any(alias in haystack for alias in aliases):
            found.setdefault(CATEGORY.get(canonical, "其它"), set()).add(canonical)

    for canonical, patterns in _SHORT_TERMS.items():
        if any(alias in haystack for alias in _SHORT_EXCLUDE):
            continue
        if any(re.search(pattern, haystack) for pattern in patterns):
            found.setdefault(CATEGORY.get(canonical, "语言"), set()).add(canonical)

    return {
        "by_category": {name: sorted(values) for name, values in sorted(found.items())},
        "flat": sorted({term for values in found.values() for term in values}),
    }


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)


def load(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def backfill(db_path: str | Path, *, limit: int = 0) -> int:
    """给缺 `tech_stack_json` 的岗位补技术栈。**纯规则抽取，零 LLM 成本。**

    为什么单独做一遍而不是只挂在打标流程里：库里存量 7,417 条有标签但
    `tech_stack_json` 是旧脚本留下的，新岗位又是空的；重跑打标要花 LLM 钱，
    而技术栈抽取只是词表匹配，秒级完成。两件事的成本差两个数量级。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        sql = ("SELECT canonical_id, job_description FROM jobs "
               "WHERE job_description IS NOT NULL AND TRIM(job_description) != '' "
               "AND (tech_stack_json IS NULL OR TRIM(tech_stack_json) = '' "
               "     OR TRIM(tech_stack_json) = '{}')")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
        if not rows:
            return 0
        conn.executemany(
            "UPDATE jobs SET tech_stack_json = ? WHERE canonical_id = ?",
            [(to_json(extract(jd)), cid) for cid, jd in rows],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="回填 tech_stack_json（纯规则，不调模型）")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "data" / "hangzhou_agent.sqlite3"))
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    n = backfill(args.db, limit=args.limit)
    print(f"已回填 {n} 条技术栈")

