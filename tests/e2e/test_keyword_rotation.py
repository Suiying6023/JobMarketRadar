"""关键词轮转：不再每轮都打配置里的前 N 个词。

原来的实现是 `config.keywords[:max_keywords_per_run]` —— 连跑四轮，插入数
154 → 217 → 49 → 31，因为从第二轮起就在翻同一批结果。配置里 6 个词，
后 3 个从来没被采过。
"""

from __future__ import annotations

from job_radar.models import CollectionConfig, CollectionStats, Platform
from job_radar.rounds import query_plan, rotate_keywords
from job_radar.storage import JobStore


def test_never_collected_keywords_come_first():
    keywords = ["AI Agent", "智能体开发", "大模型应用开发", "RAG", "Python"]
    assert rotate_keywords(keywords, {}, 3) == ["AI Agent", "智能体开发", "大模型应用开发"]


def test_least_recently_used_is_picked_first():
    keywords = ["A", "B", "C", "D"]
    recency = {"A": "2026-10-19 10:00", "B": "2026-10-18 10:00"}
    # C、D 没采过 → 排最前；A 最近采过 → 排最后
    assert rotate_keywords(keywords, recency, 2) == ["C", "D"]
    assert rotate_keywords(keywords, recency, 4) == ["C", "D", "B", "A"]


def test_ties_keep_the_configured_order():
    """时间相同就按配置顺序，避免每轮顺序抖动。"""
    keywords = ["A", "B", "C"]
    recency = {"A": "2026-10-19 10:00", "B": "2026-10-19 10:00", "C": "2026-10-19 10:00"}
    assert rotate_keywords(keywords, recency, 2) == ["A", "B"]


def test_touch_advances_recency_without_clobbering_paging():
    """只推时间，不动 last_page / exhausted —— 那是深层续翻用的另一件事。"""
    with JobStore(":memory:") as store:
        store.set_keyword_progress("杭州", "RAG", last_page=7, exhausted=True, last_new=3)
        store.touch_keyword_progress("杭州", "RAG")

        row = store.conn.execute(
            "SELECT last_page, exhausted, last_new, updated_at FROM keyword_progress "
            "WHERE city='杭州' AND keyword='RAG'").fetchone()
        assert row[0] == 7 and row[1] == 1 and row[2] == 3, "翻页进度不能被轮转覆盖"
        assert row[3], "时间必须被推进"

        assert "RAG" in store.keyword_recency()


def test_recency_takes_the_latest_across_cities():
    with JobStore(":memory:") as store:
        store.conn.execute(
            "INSERT INTO keyword_progress(city,keyword,last_page,exhausted,last_new,updated_at)"
            " VALUES ('杭州','RAG',0,0,0,'2026-10-01 00:00:00')")
        store.conn.execute(
            "INSERT INTO keyword_progress(city,keyword,last_page,exhausted,last_new,updated_at)"
            " VALUES ('上海','RAG',0,0,0,'2026-10-19 00:00:00')")
        store.conn.commit()
        assert store.keyword_recency()["RAG"] == "2026-10-19 00:00:00"


def test_query_plan_rotates_keywords():
    """轮转无条件生效：它与请求预算无关，是「每个词都该轮到」的公平性要求。"""
    cfg = CollectionConfig.model_validate({
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["A", "B", "C", "D"],
        "max_keywords_per_run": 2,
        "pages_per_query": 1,
    })
    plan = query_plan(cfg, recency={"A": "2026-10-19 10:00", "B": "2026-10-19 11:00"})
    assert [item["keyword"] for item in plan] == ["C", "D"]
