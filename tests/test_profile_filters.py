"""画像筛选口径测试。

覆盖 2026-08-10 重构修掉的几个静默缺陷：
- clean_local_db 标题/公司黑名单的 NameError（match_keyword 取代 any(...)）
- 活跃度中间档落进裂缝（改按 recruiter_active_days 分档）
- tech_match 集合精确交集只命中 23%（改子串计数）
- risk_tags 空工商数据静默判为无风险
- 薪资分桶不互斥（起薪>20K 仍出现在高薪页）
- salary_mid 忽略 ·13薪 折算
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_pool_html as E  # noqa: E402
from job_radar import profile as P  # noqa: E402
from job_radar.normalization import active_days_from_desc  # noqa: E402


def row(**kw) -> dict:
    """构造一条最小 jobs 行（dict 支持 r["k"] 取值，与 sqlite3.Row 同接口）。"""
    base = {
        "canonical_id": "boss:x", "title": "AI Agent 开发工程师", "company_name": "示例科技",
        "city": "杭州", "salary_text": "10-15K", "salary_min_k": 10.0, "salary_max_k": 15.0,
        "salary_months": 12, "experience": "经验不限", "education": "本科",
        "company_size": "100-499人", "financing_stage": "A轮", "job_invalid": 0,
        "job_status_desc": "", "is_proxy_job": 0, "recruiter_title": "HR",
        "recruiter_active_status": "刚刚活跃", "recruiter_active_days": 0,
        "recruiter_certificated": 1, "welfare_json": "[]", "skills_json": "[]",
        "glm_tags_json": "{}", "company_reg_json": "{}", "brand_id": "b1",
        "job_description": "jd", "company_intro": "", "district": "西湖区",
        "job_url": "u", "recruiter_id": "r1", "raw_json": "{}",
        # 时效字段：默认取今天，让不关心时效的用例不被陈旧扣分干扰
        "last_seen_at": date.today().isoformat(),
        "detail_fetched_at": date.today().isoformat(),
    }
    base.update(kw)
    return base


# ---------- match_keyword：取代会 NameError 的 any(...) 写法 ----------

def test_match_keyword_returns_the_hit_not_nameerror():
    assert P.match_keyword("Python开发（外包驻场）", P.EXCLUDE_TITLE) == "外包"
    assert P.match_keyword("杭州某科技有限公司", P.EXCLUDE_COMPANY) is None
    assert P.match_keyword("软通动力信息技术", P.EXCLUDE_COMPANY) == "软通动力"


def test_match_keyword_is_case_insensitive():
    assert P.match_keyword("AIGC设计师", P.EXCLUDE_TITLE) == "AIGC"
    assert P.match_keyword("aigc设计师", P.EXCLUDE_TITLE) == "AIGC"


# ---------- 活跃度分档：中间档不再落进裂缝 ----------

def test_online_counts_as_most_active_not_missing():
    """「在线」原先返回 None，与真正的缺失值混在一起。"""
    assert active_days_from_desc("在线") == 0
    assert active_days_from_desc("近半年活跃") == 180
    assert active_days_from_desc("") is None


def test_middle_activity_tier_lands_in_p2_not_nowhere():
    assert E.active_tier(row(recruiter_active_days=0)) == "p0"
    assert E.active_tier(row(recruiter_active_days=3)) == "p0"
    # 原先「本周/2周内/本月...」这批 170 条既不属 P0 也不属 P1
    assert E.active_tier(row(recruiter_active_days=7)) == "p2"
    assert E.active_tier(row(recruiter_active_days=180)) == "p2"
    assert E.active_tier(row(recruiter_active_days=None)) == "p1"


# ---------- 薪资：月数折算 + 四桶互斥 ----------

def test_salary_mid_accounts_for_extra_months():
    r13 = row(salary_min_k=13.0, salary_max_k=17.0, salary_months=13)
    assert E.salary_mid(r13) == 15.0 * 13 / 12   # 16.25，跨过 15K 高薪线
    assert E.salary_mid(row(salary_months=None)) == 12.5   # None 视作 12 薪


def test_salary_buckets_are_mutually_exclusive():
    over = row(salary_min_k=25.0, salary_max_k=40.0)
    high = row(salary_min_k=16.0, salary_max_k=20.0)
    low = row(salary_min_k=5.0, salary_max_k=8.0)
    core = row(salary_min_k=10.0, salary_max_k=15.0)
    assert [E.salary_bucket(r) for r in (over, high, low, core)] == \
        ["over", "high", "low", "core"]


def test_over_threshold_job_never_lands_in_high_page():
    """原先 p0_high 在剔除起薪>20K 之前算出，28 条超纲岗混进高薪页。"""
    assert E.salary_bucket(row(salary_min_k=40.0, salary_max_k=65.0)) == "over"


# ---------- 风险标记：空工商数据不再静默通过 ----------

def test_empty_registration_is_flagged_not_silently_safe():
    assert E.risk_tags({}) == ["工商未知"]


def test_dash_capital_is_flagged():
    assert "资本未知" in E.risk_tags({"注册资本": "-", "成立时间": "2010-01-01"})


def test_low_capital_and_new_company_and_non_corporate():
    tags = E.risk_tags({
        "注册资本": "50万人民币",
        "成立时间": date.today().isoformat(),
        "企业类型": "个体工商户",
    })
    assert set(tags) == {"低资本", "新成立", "非公司主体"}


def test_foreign_currency_capital_is_converted():
    """5000 万美元 ≈ 3.6 亿人民币，不该因币种差异误判。"""
    assert E.cap_wan("5000万美元") > E.cap_wan("5000万人民币")
    assert E.risk_tags({"注册资本": "5000万美元", "成立时间": "2010-01-01"}) == []


def test_new_company_cutoff_is_relative_not_hardcoded():
    """原先写死 '2025-08-09'，2026-11 再跑会变成「距今 15 个月」。"""
    assert P.risk_cutoff_date(date(2026, 8, 10)) < "2026-08-10"
    assert P.risk_cutoff_date(date(2027, 1, 1)) > P.risk_cutoff_date(date(2026, 8, 10))


# ---------- 薪资：按区间均值判，不按起薪 ----------

def test_salary_threshold_uses_mid_not_min():
    """按起薪判会把「22-28K」这类实际可投的岗位剔掉，按均值才符合口径。"""
    # 起薪 22K 超旧的 20K 线，但均值 25K 不超新线 → 应保留
    assert P.salary_out_of_range(22.0, 28.0) is False
    # 起薪 20K 不超旧线，但 ·14薪 折算后均值 26.2K 超新线 → 应剔除
    assert P.salary_out_of_range(20.0, 25.0, 14) is True
    # 缺值不判超纲（不能因为平台没给薪资就丢掉）
    assert P.salary_out_of_range(None, None) is False


def test_salary_mid_folds_in_extra_months():
    """`15-25K·15薪` 的实际年薪高于 `18-28K·12薪`，忽略月数会把前者当低薪。"""
    assert P.salary_mid(15.0, 25.0, 15) > P.salary_mid(18.0, 28.0, 12)
    assert P.salary_mid(10.0, 20.0) == 15.0          # months 缺省按 12
    assert P.salary_mid(None, 20.0) is None


def test_over_bucket_is_evaluated_before_high():
    """超纲必须先判，否则超上限的岗位会先落进高薪页，与副标题口径矛盾。"""
    assert E.salary_bucket(row(salary_min_k=20.0, salary_max_k=25.0,
                               salary_months=14)) == "over"


# ---------- 数据时效 ----------

def test_stale_penalty_decays_with_observation_age():
    """久未核实代表下架风险，随观测年龄线性加重。"""
    assert P.stale_penalty(0) == 0.0
    assert P.stale_penalty(P.STALE_FRESH_DAYS) == 0.0
    assert P.stale_penalty(P.STALE_FULL_DAYS) == P.W_STALE_PENALTY
    assert P.stale_penalty(999) == P.W_STALE_PENALTY
    # 中间段单调递减
    mid = P.stale_penalty((P.STALE_FRESH_DAYS + P.STALE_FULL_DAYS) // 2)
    assert P.W_STALE_PENALTY < mid < 0.0
    # 从未观测按扣满处理——无从判断新鲜度的岗位不该占榜首
    assert P.stale_penalty(None) == P.W_STALE_PENALTY


def test_freshness_expires_snapshot_to_zero_not_negative():
    """过期的活跃度是**无信息**，不是坏消息，所以衰减到 0 而非负数。"""
    assert P.freshness(0) == 1.0
    assert P.freshness(P.STALE_FRESH_DAYS) == 1.0
    assert P.freshness(P.STALE_FULL_DAYS) == 0.0
    assert P.freshness(999) == 0.0
    assert P.freshness(None) == 0.0        # 从未观测同样是无信息
    assert 0.0 < P.freshness((P.STALE_FRESH_DAYS + P.STALE_FULL_DAYS) // 2) < 1.0


def test_stale_penalty_stays_smaller_than_activity_weight():
    """下架风险扣分必须小于活跃度权重。

    否则过期的「在线」要靠扣分去抵，而扣分对技术栈匹配这类**与时间无关**的
    项同样生效 —— 一个 19 天前采到的高对口岗位会被压到低对口岗位下面。
    过期活跃度应由 freshness() 归零，不是由扣分抵消。
    """
    assert abs(P.W_STALE_PENALTY) < P.W_ACT_MAX


def test_stale_job_ranks_below_fresh_equivalent():
    """同等条件下，久未核实的岗位得分必须更低。"""
    fresh = row(last_seen_at=date.today().isoformat(),
                detail_fetched_at=date.today().isoformat())
    stale = row(last_seen_at="2026-01-01", detail_fetched_at="2026-01-01")
    assert E.job_score(fresh) > E.job_score(stale)


def test_staleness_does_not_outrank_tech_match():
    """陈旧不该压过对口：一条旧但高对口的岗位仍要排在新但不对口的前面。

    对口与否是**与时间无关**的判断——记录旧了不会让 langgraph 岗变得不对口。
    原先陈旧扣分 -2.0 大于活跃度权重 1.0，正是把这个反过来了。
    """
    stale_relevant = row(
        last_seen_at="2026-01-01", detail_fetched_at="2026-01-01",
        glm_tags_json='{"tech_req": ["python", "langgraph", "rag", "agent"]}')
    fresh_irrelevant = row(
        last_seen_at=date.today().isoformat(),
        detail_fetched_at=date.today().isoformat(),
        glm_tags_json='{"tech_req": []}')
    assert E.job_score(stale_relevant) > E.job_score(fresh_irrelevant)


def test_expired_online_status_scores_like_unknown_not_worse():
    """过期的「在线」应退化成与「没有活跃度数据」同级，而不是更差。"""
    expired_online = row(last_seen_at="2026-01-01", detail_fetched_at="2026-01-01",
                         recruiter_active_status="在线")
    expired_blank = row(last_seen_at="2026-01-01", detail_fetched_at="2026-01-01",
                        recruiter_active_status="")
    assert E.job_score(expired_online) == E.job_score(expired_blank)


def test_last_verified_takes_newer_of_two_stamps():
    """存量数据里有 last_seen_at 早于 detail_fetched_at 的行，必须取较新者。"""
    assert E.last_verified(row(last_seen_at="2026-08-01",
                               detail_fetched_at="2026-08-20")) == "2026-08-20"
    assert E.last_verified(row(last_seen_at="", detail_fetched_at="")) == ""


# ---------- 评分：技术栈子串匹配 ----------

def test_tech_match_hits_full_sentence_tech_req():
    """LLM 返回的是整句 tech_req，集合精确交集会一律判 0 分。"""
    sentence = row(glm_tags_json='{"tech_req": ['
                   '"熟悉python、java、go等至少一种后端语言",'
                   '"了解大模型应用开发基础（prompt engineering、rag、agent等）"]}')
    plain = row(glm_tags_json='{"tech_req": []}')
    assert E.job_score(sentence) > E.job_score(plain)


def test_tech_match_is_graded_not_binary():
    """命中数越多分越高，能区分「只提 python」和「langgraph+rag+微调全都要」。"""
    one = row(glm_tags_json='{"tech_req": ["python"]}')
    many = row(glm_tags_json='{"tech_req": ["python", "langgraph", "rag", "微调", "vllm"]}')
    assert E.job_score(many) > E.job_score(one)


def test_activity_no_longer_dominates_score():
    """页内活跃度差距不该压过技术栈匹配（原先 act 权重 3.0 vs tech 2.0）。"""
    online_no_tech = row(recruiter_active_status="在线", glm_tags_json='{"tech_req": []}')
    stale_full_tech = row(
        recruiter_active_status="3日内活跃",
        glm_tags_json='{"tech_req": ["python", "langgraph", "rag", "微调"]}')
    assert E.job_score(stale_full_tech) > E.job_score(online_no_tech)


def test_missing_financing_stage_is_not_worst_score():
    """缺失值原先与「未融资」同分 0.8。"""
    assert P.STAGE_SCORE.get("", P.STAGE_DEFAULT) > P.STAGE_SCORE["未融资"]


def test_sales_welfare_is_penalty_not_hard_filter():
    """「底薪加提成」多为公司统一勾选的福利模板（实测 67 条含浙大/大厂，
    JD 无销售内容者居多），所以只降分、不剔除。"""
    r = row(welfare_json='["五险一金", "底薪加提成"]')
    assert E.hard_drop(r) == ""
    assert E.job_score(r) < E.job_score(row())


# ---------- 硬过滤 ----------

def test_hard_drop_catches_what_previously_leaked_into_p0():
    assert E.hard_drop(row(job_invalid=1)) == "已失效"
    assert E.hard_drop(row(job_status_desc="职位已关闭")) == "已关闭"
    assert E.hard_drop(row(city="成都")).startswith("非目标城市")
    assert E.hard_drop(row(recruiter_title="猎头顾问")) == "猎头发布"
    assert E.hard_drop(row(salary_text="200元/天")) == "日薪(实习)"
    assert E.hard_drop(row()) == ""


def test_whitelisted_cities_pass_hard_drop():
    """杭州/上海/深圳都在投递池范围内；名单外的城市才剔除。"""
    for city in ("杭州", "上海", "深圳"):
        assert E.hard_drop(row(city=city)) == "", city
    assert P.city_score("杭州") > P.city_score("上海") > P.city_score("成都")


def test_anonymous_detected_by_field_then_name():
    """匿名判定优先用结构化 raw.anonymous，公司名正则只作兜底。"""
    # 字段命中，名字里没有「某」——纯字符串匹配会漏掉这类
    assert E.hard_drop(row(company_name="知名互联网公司",
                           raw_json='{"anonymous": 100}')) == "匿名公司"
    # 无字段时靠名字兜底（老库 raw 可能已被列表轮次覆盖）
    assert E.hard_drop(row(company_name="深圳某大型计算机软件公司",
                           raw_json="{}")) == "匿名公司"
    # 「某」不在开头的真实公司名不该被误杀
    assert P.is_anonymous({}, "杭州果集某科技") is False
    assert P.is_anonymous({"anonymous": 0}, "阿里巴巴") is False


def test_future_cohort_uses_profile_year():
    assert E.hard_drop(row(title="AI应用开发工程师【27届校招】")) == "未来届"
    assert E.hard_drop(row(title="AI应用开发工程师【2027届校招】")) == "未来届"
    assert E.hard_drop(row(title="智能体研发工程师（26届）")) == ""


def test_bare_cv_keyword_does_not_kill_llm_research_job():
    """tech_req=['NLP/CV/模型压缩'] 是备选列表，不是 CV 要求。"""
    assert E.hard_drop(row(glm_tags_json='{"tech_req": ["NLP/CV/模型压缩"]}')) == ""
    assert E.hard_drop(row(glm_tags_json='{"tech_req": ["CV"]}')) == "CV/图像算法"
    assert E.hard_drop(row(title="机器视觉算法工程师")) == "CV/图像算法"


# ---------- 重复岗位去重 ----------

def test_dedupe_keeps_freshest_of_reposted_jobs():
    """同一岗位被 BOSS 以不同 encryptJobId 重复发布，canonical_id 抓不到。"""
    stale = row(canonical_id="boss:a", recruiter_active_days=30)
    fresh = row(canonical_id="boss:b", recruiter_active_days=0)
    other = row(canonical_id="boss:c", title="别的岗位")
    kept, dup = E.dedupe([stale, fresh, other])
    assert dup == 1
    assert {r["canonical_id"] for r in kept} == {"boss:b", "boss:c"}
