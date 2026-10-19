"""节流叠加与详情去重前置的测试。

覆盖 2026-08-10 的两处采集链路修改：
- 突发惩罚改为叠加在常规延迟之上（原先 return 跳过常规延迟，惩罚可能反而更短）
- 详情去重前置：库里已有 JD 的岗位不再重复抓详情（实测 7258 次重复机会）
"""
from __future__ import annotations

import asyncio
import json

from job_radar.models import CityConfig, CollectionConfig, JobRecord, Platform
from job_radar.storage import JobStore
from job_radar.throttle import gaussian, human_pause


def make_config(**kw) -> CollectionConfig:
    base = dict(
        cities=[CityConfig(name="杭州", boss_code="101210100")],
        keywords=["AI Agent"],
        request_delay_seconds=(1.0, 2.0),
        detail_delay_seconds=(3.0, 4.0),
    )
    base.update(kw)
    return CollectionConfig(**base)


class SleepRecorder:
    """替换 asyncio.sleep，记录每次等待时长而不真的等。"""

    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


# ---------- 高斯延迟 ----------

def test_gaussian_stays_in_range():
    for _ in range(200):
        assert 5.0 <= gaussian(5.0, 9.0) <= 9.0


def test_gaussian_handles_zero_width_range():
    assert gaussian(3.0, 3.0) == 3.0


def test_gaussian_does_not_hang_on_inverted_range():
    """yaml 里把区间写反（[12, 8]）原先会死循环，整轮采集静默卡死。"""
    for _ in range(50):
        assert 8.0 <= gaussian(12.0, 8.0) <= 12.0


# ---------- 拟人抖动 ----------

def test_human_pause_mean_matches_range_midpoint():
    """抖动的形状可以乱，**均值必须守住区间中点** —— 速率是唯一实测过安全区间的量。"""
    samples = [human_pause(6.0, 11.0) for _ in range(4000)]
    mean = sum(samples) / len(samples)
    assert abs(mean - 8.5) < 0.4, f"均值 {mean:.2f} 偏离中点 8.5 过多"


def test_human_pause_never_goes_below_configured_floor():
    """`[6,11]` 就该真的不短于 6 秒 —— 地板是运维设的安全参数，不能被抖动吃掉。

    第一版用"相对中点的倍数"实现，抽出了 5.04 秒：均值守住了，配置语义却变了。
    """
    samples = [human_pause(6.0, 11.0) for _ in range(4000)]
    assert min(samples) >= 6.0, f"最小值 {min(samples):.2f} 跌破下限 6.0"
    assert min(samples) < 6.5, "下限附近应当有样本，否则等于没抖动"


def test_human_pause_has_long_tail_but_bounded_floor():
    """右偏长尾：中位数低于均值，最大值明显超过配置上界，下界不破。"""
    samples = sorted(human_pause(6.0, 11.0) for _ in range(4000))
    median = samples[len(samples) // 2]
    assert median < 8.5, "中位数应低于均值（右偏）"
    assert samples[-1] > 11.0, "长尾应能超过配置上界"
    assert samples[0] >= 6.0, "下界不得跌破配置下限"


def test_human_pause_handles_inverted_and_zero_range():
    for _ in range(50):
        assert human_pause(12.0, 8.0) >= 8.0, "区间写反后下限应是较小的那个数"
    assert human_pause(0.0, 0.0) == 0.0


# ---------- 详情去重前置 ----------

def job(job_id: str, jd: str = "") -> JobRecord:
    return JobRecord(
        platform=Platform.BOSS, source_job_id=job_id,
        job_url=f"https://www.zhipin.com/job_detail/{job_id}.html",
        title="AI Agent 开发工程师", company_name="示例科技", city="杭州",
        job_description=jd,
    )


def test_detailed_ids_returns_only_jobs_with_jd(tmp_path):
    db = tmp_path / "t.sqlite3"
    with JobStore(db) as store:
        store.upsert(job("with", jd="岗位职责：开发 Agent"))
        store.upsert(job("without"))
        assert store.detailed_ids() == {"boss:with"}


def test_detailed_ids_is_empty_on_fresh_db(tmp_path):
    with JobStore(tmp_path / "t.sqlite3") as store:
        assert store.detailed_ids() == set()


def test_skip_set_matches_canonical_key(tmp_path):
    """采集器用 job.canonical_key() 查跳过集合，两边必须是同一种键。"""
    db = tmp_path / "t.sqlite3"
    j = job("abc", jd="有 JD")
    with JobStore(db) as store:
        store.upsert(j)
        assert j.canonical_key() in store.detailed_ids()


# ---------- raw_json merge：列表轮次不再抹掉 raw.detail ----------

def detail_job(job_id: str = "X1") -> JobRecord:
    """模拟详情路径产出：raw 里有 detail 块和轮换后的新 securityId。"""
    j = job(job_id, jd="JD 正文")
    j.detail_fetched_at = "2026-08-10T00:00:00Z"
    j.raw = {
        "listField": "old",
        "detail": {"postDescription": "JD 正文"},
        "securityId": "SEC-NEW",
        "lid": "LID-NEW",
    }
    return j


def test_list_round_does_not_wipe_raw_detail(tmp_path):
    """原先 raw_json 在 OVERWRITE_COLUMNS 里被裸覆盖：下一轮列表采集用「纯列表
    raw」盖掉「含 detail 的完整 raw」，实测已抹掉 639/1891 条（34%）的 raw.detail，
    连轮换后的新 securityId/lid 一起丢。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        store.upsert(detail_job())
        list_only = job("X1")           # 列表路径：raw 只有列表字段
        list_only.raw = {"listField": "new"}
        assert store.upsert(list_only) == "updated"

        raw = json.loads(store.conn.execute("SELECT raw_json FROM jobs").fetchone()[0])
        assert "detail" in raw                    # detail 块存活
        assert raw["securityId"] == "SEC-NEW"     # 新鲜 token 存活
        assert raw["lid"] == "LID-NEW"
        assert raw["listField"] == "new"          # 列表字段仍然更新为新值


def test_detail_fetched_at_survives_list_round(tmp_path):
    """detail_fetched_at 走 KEEP_IF_EMPTY 语义：列表轮次传空串不该清掉它。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        store.upsert(detail_job())
        store.upsert(job("X1"))       # 列表路径，detail_fetched_at 为空
        row = store.conn.execute(
            "SELECT detail_fetched_at, job_description FROM jobs").fetchone()
        assert row["detail_fetched_at"] == "2026-08-10T00:00:00Z"
        assert row["job_description"] == "JD 正文"


def test_detail_only_columns_survive_list_round(tmp_path):
    """详情专属列不能被列表轮次的空值抹掉。

    这个坑踩了**两次**：2026-08-10 补了 recruiter_active_status 等五个字段，
    却漏了 job_status_desc/position_name/recruitment_count —— 它们同样只有详情
    接口给（`_parse_api_job` 完全不赋值），继续被抹了一个月。
    抹除率按 seen_count 单调爬升是当时的决定性证据：
      seen=2 → 0%(1/749)   seen=3-4 → 63%   seen=5-7 → 71%   seen=8+ → 85%
    这条测试就是防第三次。
    """
    with JobStore(tmp_path / "t.sqlite3") as store:
        detailed = detail_job()
        detailed.job_status_desc = "招聘中"
        detailed.position_name = "AI Agent 开发"
        detailed.recruitment_count = "招3人"
        store.upsert(detailed)

        # 列表轮次：这三列是模型默认空串
        assert store.upsert(job("X1")) == "updated"

        row = store.conn.execute(
            "SELECT job_status_desc, position_name, recruitment_count FROM jobs"
        ).fetchone()
        assert row["job_status_desc"] == "招聘中"
        assert row["position_name"] == "AI Agent 开发"
        assert row["recruitment_count"] == "招3人"


def test_detail_only_columns_still_updatable_by_detail(tmp_path):
    """保护不能变成"永远写不进新值"——详情轮次给了非空值就该覆盖。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        first = detail_job()
        first.job_status_desc = "招聘中"
        store.upsert(first)

        again = detail_job()
        again.job_status_desc = "已结束"     # 岗位状态真的变了
        store.upsert(again)

        assert store.conn.execute(
            "SELECT job_status_desc FROM jobs").fetchone()[0] == "已结束"


def test_recruiter_online_still_overwrites(tmp_path):
    """recruiter_online 相反：列表确实返回 bossOnline，且"当前在线"是会来回变的
    实时状态，做单向保护会把旧的 True 永久钉住。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        online = detail_job()
        online.recruiter_online = True
        store.upsert(online)

        offline = job("X1")               # 列表轮次，默认 False
        offline.recruiter_online = False
        store.upsert(offline)

        assert store.conn.execute(
            "SELECT recruiter_online FROM jobs").fetchone()[0] == 0


def test_detail_round_can_refresh_its_own_raw(tmp_path):
    """详情重抓时，新的 detail 块要能覆盖旧的（merge 是新值优先）。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        store.upsert(detail_job())
        again = detail_job()
        again.raw["detail"] = {"postDescription": "更新后的 JD"}
        again.raw["securityId"] = "SEC-ROTATED"
        store.upsert(again)
        raw = json.loads(store.conn.execute("SELECT raw_json FROM jobs").fetchone()[0])
        assert raw["detail"]["postDescription"] == "更新后的 JD"
        assert raw["securityId"] == "SEC-ROTATED"


def test_fresh_insert_keeps_raw_intact(tmp_path):
    """没有旧记录时 merge 分支不该跑，raw 原样入库。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        assert store.upsert(detail_job()) == "inserted"
        raw = json.loads(store.conn.execute("SELECT raw_json FROM jobs").fetchone()[0])
        assert raw["detail"]["postDescription"] == "JD 正文"


def test_list_round_does_not_wipe_detail_only_fields(tmp_path):
    """活跃度/认证/代招/失效 只有详情接口给（列表接口不返回 activeTimeDesc），
    原先它们在 OVERWRITE_COLUMNS 里被列表轮次的空值无条件覆盖 —— 每跑一次列表
    采集就清空一批花详情配额换来的数据。2026-08-10 小批量验证时实测复现：
    1 关键词 2 页列表，12 条 active 的活跃度被抹成空。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        detailed = job("A", jd="JD")
        detailed.recruiter_active_status = "刚刚活跃"
        detailed.recruiter_active_days = 0
        detailed.recruiter_certificated = True
        detailed.is_proxy_job = True
        detailed.job_invalid = True
        store.upsert(detailed)

        store.upsert(job("A"))      # 列表轮次：这些字段都是默认空值

        r = store.conn.execute("SELECT * FROM jobs").fetchone()
        assert r["recruiter_active_status"] == "刚刚活跃"
        assert r["recruiter_active_days"] == 0
        assert r["recruiter_certificated"] == 1
        assert r["is_proxy_job"] == 1
        assert r["job_invalid"] == 1


def test_active_days_zero_is_a_real_value_not_empty(tmp_path):
    """`recruiter_active_days = 0` 表示"今天活跃过"，是**最强**的活跃信号。
    它一度被归到「0 也算空就保留旧值」的规则里，导致详情给的 0 永远写不进去
    （实测：status='刚刚活跃' 但 days=None）。0 必须能写入，只有 NULL 才保留旧值。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        store.upsert(job("A"))          # 列表先入库：days=None
        fresh = job("A")
        fresh.recruiter_active_status = "刚刚活跃"
        fresh.recruiter_active_days = 0
        store.upsert(fresh)
        assert store.conn.execute(
            "SELECT recruiter_active_days FROM jobs").fetchone()[0] == 0

        store.upsert(job("A"))          # 再来一轮列表：0 要保住，不能被 None 抹掉
        assert store.conn.execute(
            "SELECT recruiter_active_days FROM jobs").fetchone()[0] == 0


def test_detail_round_can_still_update_activity(tmp_path):
    """保护不能变成永久锁死——详情重抓要能刷新活跃度（它是时效字段）。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        first = job("A", jd="JD")
        first.recruiter_active_status = "刚刚活跃"
        first.recruiter_active_days = 0
        store.upsert(first)

        later = job("A")
        later.recruiter_active_status = "3日内活跃"
        later.recruiter_active_days = 3
        store.upsert(later)

        r = store.conn.execute(
            "SELECT recruiter_active_status, recruiter_active_days FROM jobs").fetchone()
        assert r["recruiter_active_status"] == "3日内活跃"
        assert r["recruiter_active_days"] == 3


def test_corrupt_old_raw_does_not_break_upsert(tmp_path):
    """旧 raw_json 坏了（非法 JSON）也要能继续写，不能抛异常丢掉这轮数据。"""
    with JobStore(tmp_path / "t.sqlite3") as store:
        store.upsert(detail_job())
        store.conn.execute("UPDATE jobs SET raw_json = '{not json'")
        store.conn.commit()
        fresh = job("X1")
        fresh.raw = {"listField": "new"}
        assert store.upsert(fresh) == "updated"
        raw = json.loads(store.conn.execute("SELECT raw_json FROM jobs").fetchone()[0])
        assert raw == {"listField": "new"}
