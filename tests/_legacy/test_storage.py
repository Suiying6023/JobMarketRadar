from pathlib import Path

from job_radar.models import JobRecord, Platform
from job_radar.storage import JobStore


def test_upsert_merges_queries(tmp_path: Path):
    db = tmp_path / "jobs.sqlite3"
    job = JobRecord(
        platform=Platform.BOSS,
        source_job_id="abc",
        job_url="https://example.com/abc",
        title="AI工程师",
        company_name="示例公司",
        city="杭州",
        matched_queries=["AI"],
    )
    with JobStore(db) as store:
        assert store.upsert(job) == "inserted"
        job.matched_queries = ["大模型"]
        assert store.upsert(job) == "updated"
        row = store.rows()[0]
        assert "AI" in row["matched_queries_json"]
        assert "大模型" in row["matched_queries_json"]


def test_city_summary_counts_interface_values(tmp_path: Path):
    """经验/学历口径必须用**接口返回值**，不是 BOSS 搜索参数标签。

    原先写死 `'应届生'`（搜索参数码 102 的标签）和 `'不限'`，而接口实际返回
    `'在校/应届'` 和 `'学历不限'` → 这两类岗位一条都数不进去。
    """
    db = tmp_path / "jobs.sqlite3"
    with JobStore(db) as store:
        for i, (exp, deg) in enumerate([
            ("在校/应届", "学历不限"),   # 两个旧字面量都漏掉的组合
            ("经验不限", "本科"),        # 旧字面量能命中的组合
            ("5-10年", "硕士"),          # 画像外，两边都不该数
        ]):
            store.upsert(JobRecord(
                platform=Platform.BOSS, source_job_id=f"j{i}",
                job_url=f"https://example.com/j{i}", title="AI工程师",
                company_name="示例公司", city="杭州",
                experience=exp, education=deg,
            ))

        summary = {r["city"]: r for r in store.city_summary()}["杭州"]
        assert summary["jobs"] == 3
        assert summary["junior_jobs"] == 2, "在校/应届 必须算进应届友好岗"
        assert summary["bachelor_accessible"] == 2, "学历不限 必须算进本科可投岗"
