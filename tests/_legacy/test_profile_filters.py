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

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

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
    """大小写不敏感。用公司黑名单里的英文词做用例 —— 原先用 `AIGC`，
    但它已从标题黑名单移出（改按 JD 内容判设计岗），那不是这条测试要守的事。"""
    assert P.match_keyword("外企德科数字技术", P.EXCLUDE_COMPANY) == "外企德科"
    assert P.match_keyword("PYTHON开发（外包驻场）", P.EXCLUDE_TITLE) == "外包"
    assert P.match_keyword("python开发（外包驻场）", P.EXCLUDE_TITLE) == "外包"


# ---------- OD（华为外包标记）必须按词边界判，否则吃掉 Coding ----------

def test_od_marker_is_detected():
    """OD 是华为体系的外包用工标记，性质等同「外包/外派」。"""
    for t in ("数据库内核开发工程师（OD）", "【od】ai软件开发-python",
              "OD招聘 华为数据库", "后端开发工程师-华为od",
              "AI agent/java开发工程师-OD招聘", "数据库管理员(OD)"):
        assert P.outsourced_od(t), f"未识别 OD 标记: {t}"


def test_od_must_not_swallow_coding():
    """⚠️ 这是这条规则的**全部难点**：裸子串 `OD` 会命中 `Coding`。

    实测（2026-08-29）：库里标题含裸子串 `OD` 的有 91 条，其中绝大多数是
    `AI Coding Agent 研发工程师`、`Vibe Coding 全栈工程师`、
    `淘天-AI Agent应用开发工程师-coding方向` 这类 —— 那恰恰是求职者**最对口**
    的方向。按词边界匹配则精准命中 20 条，全是华为 OD 岗。

    所以 OD 绝不能进 `EXCLUDE_TITLE`（那里走裸子串匹配）。
    这条测试钉死这个边界：把它改回子串匹配就会红。
    """
    for t in ("AI Coding Agent 研发工程师", "Vibe Coding 全栈工程师",
              "淘天-AI Agent应用开发工程师-coding方向", "AI开发工程师（AI coding）",
              "全栈开发工程师｜AI Code 方向｜", "Node.js 后端工程师（AI Travel Agent ）",
              "AI Agent研发工程师-QoderWake"):
        assert not P.outsourced_od(t), f"误伤对口岗位: {t}"
    # 也不该进裸子串黑名单 —— 那等于绕过词边界
    assert "OD" not in P.EXCLUDE_TITLE, "OD 进了裸子串黑名单，会吃掉 Coding"


def test_explicit_non_od_is_allowed():
    """`华为社招-数据库内核研发-非od` 明确说了不是 OD，不该被降。"""
    assert not P.outsourced_od("华为社招-数据库内核研发-非od")
    assert not P.outsourced_od("数据库开发（非 OD 正编）")


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
    # 均值 27.5K 超线 → 应剔除
    assert P.salary_out_of_range(25.0, 30.0) is True
    # 缺值不判超纲（不能因为平台没给薪资就丢掉）
    assert P.salary_out_of_range(None, None) is False


def test_salary_threshold_ignores_extra_months():
    """⚠️ 门槛用**月基薪**，不折算 salary_months —— 与排序口径刻意不同。

    "够不着"看的是月薪 offer 能不能谈到，年终奖倍数不改变这件事。
    实测（2026-08-29）混用的代价：折算把 **475 条** raw≤25 的推过门槛、
    **反向 0 条**（单向抬升），其中 267 条"超纲"是唯一降级理由。
    典型受害者标题里明写着"26届可投""经验不限"：
      `15-30K·14薪` 月基薪 22.5K → 折算 26.2K 被判超纲
      `12-15K·24薪` 月基薪 13.5K → 折算 27.0K 被判超纲
    """
    # 月基薪 22.5K 不超线，14 薪也不该让它超
    assert P.salary_out_of_range(15.0, 30.0, 14) is False
    # 极端倍数：月基薪 13.5K，24 薪折算 27K —— 仍不该判超纲
    assert P.salary_out_of_range(12.0, 15.0, 24) is False
    # months 参数在签名里但不生效，传什么都一样
    assert (P.salary_out_of_range(20.0, 25.0, 12)
            == P.salary_out_of_range(20.0, 25.0, 16))


def test_ranking_still_folds_in_extra_months():
    """排序/分桶口径保持折算 —— 两个口径必须都在，别为了统一而砍掉一个。"""
    assert P.salary_mid(15.0, 30.0, 14) > P.salary_raw_mid(15.0, 30.0)
    assert P.salary_raw_mid(15.0, 30.0) == 22.5
    assert P.salary_raw_mid(None, 20.0) is None


def test_fresh_grad_experience_value_is_allowed():
    """接口有时直接返回「应届生」而非「在校/应届」，漏掉它语义就反了。

    实测库里 5 种 experience 取值：1-3年 / 经验不限 / 1年以内 / 在校/应届 / 应届生。
    最后一个原先不在白名单，会被判「经验不符」降级 —— 而它恰恰最该保留。
    """
    for v in ("应届生", "在校/应届", "经验不限", "1年以内", "1-3年"):
        assert v in P.ALLOW_EXP, f"{v!r} 该在经验白名单里"


def test_daily_salary_is_detected_by_value_not_text():
    """日薪判据改看数值：`salary_text` 里从来没有「元/天」。

    实测全库 8109 条 salary_text 只有 `N-NK` 和 `N-NK·N薪` 两种形态，
    含「天」或「日」的 **0 条** —— 原来那条判据是死代码，
    而且写在两层里，看着像"有在防"实则从未生效。
    """
    assert P.is_daily_salary(0.3, 0.5) is True      # 300-500 元/天
    assert P.is_daily_salary(3.0, 4.0) is False     # 3-4K 月薪，低但不是日薪
    assert P.is_daily_salary(None, None) is False


def test_salary_mid_folds_in_extra_months():
    """`15-25K·15薪` 的实际年薪高于 `18-28K·12薪`，忽略月数会把前者当低薪。"""
    assert P.salary_mid(15.0, 25.0, 15) > P.salary_mid(18.0, 28.0, 12)
    assert P.salary_mid(10.0, 20.0) == 15.0          # months 缺省按 12
    assert P.salary_mid(None, 20.0) is None


def test_over_bucket_is_evaluated_before_high():
    """超纲必须先判，否则超上限的岗位会先落进高薪页，与副标题口径矛盾。

    用例要挑一个**月基薪**就超线的（均值 27.5K > 25K）：门槛口径已改成不折算
    月数，原先靠 `·14薪` 把 22.5K 抬到 26.2K 的写法测的是那个已被否决的口径。
    这条测试守的是**判定顺序**，不是薪资换算 —— 换算由
    test_salary_threshold_ignores_extra_months 守。
    """
    b = E.salary_bucket(row(salary_min_k=25.0, salary_max_k=30.0))
    assert b == "over", f"月基薪均值 27.5K 应判 over，实际 {b}"
    # 同一条若只看 high 的条件（mid > 15）也成立，所以顺序错了就会返回 high
    assert E.salary_mid(row(salary_min_k=25.0, salary_max_k=30.0)) > 15.0


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


def test_sales_welfare_never_hard_filters():
    """「底薪加提成」是公司统一勾选的福利模板，绝不能硬过滤。

    2026-08-29 起它连**弱负分**也取消了（`W_SALES_PENALTY=0`）：
    388 条带标记的岗位 demote 率 63.1% vs 基线 53.4%，偏差 +9.8pt，
    按本项目的判据（不足 16pt 视为无区分度）够不上权重；
    而实测代价是让 193 条平均下沉 82 个排名位，受害者包括
    `大模型算法工程师`（纯算法岗）、`AI Agent 算法工程师（应届毕业生）`。
    真正相关的是"福利勾得多"（吃罚的平均勾 11.7 项 vs 干净的 6.0 项），
    不是"岗位是销售岗"。

    这条测试守的是**不硬过滤**这条底线 —— 那部分永远成立。
    权重取值由 test_retired_weights_stay_at_zero 单独守。
    """
    r = row(welfare_json='["五险一金", "底薪加提成"]')
    assert E.hard_drop(r) == ""
    assert E.job_score(r) == E.job_score(row()), "已取消权重，不该再影响分数"


def test_retired_weights_stay_at_zero():
    """两个被否决的权重要留在 0，别被"看着像漏填"而重新填上。

    保留常量而非删除，是为了让"评估过并否决"留在代码里；
    但那也意味着下一个人可能以为是漏填。用测试固定住这个决定。
    """
    assert P.W_SALES_PENALTY == 0.0, "见 SALES_WELFARE_MARKS 注释：偏差仅 +9.8pt"
    assert P.W_UNCERT_PENALTY == 0.0, "827 条 active+有JD 里只有 6 条未认证"


def test_act_score_covers_every_status_phrase():
    """活跃度文案必须全覆盖，缺档一律得 0 分 = 让「本周活跃」等于「半年前活跃」。

    实测（2026-08-29）库里 active+有 JD 的 14 种文案，原表只有 4 种，
    263 条（31.8%）落进裂缝。这是 export_pool_html 记过的「中间档落进裂缝」
    那个 bug 的打分侧残留 —— 分页早已按天数判，打分还在按文案查表。
    """
    for phrase in ("在线", "刚刚活跃", "今日活跃", "3日内活跃", "本周活跃",
                   "2周内活跃", "本月活跃", "2月内活跃", "3月内活跃",
                   "4月内活跃", "5月内活跃", "近半年活跃", "半年前活跃"):
        assert phrase in P.ACT_SCORE, f"活跃度文案 {phrase!r} 未覆盖，会被判 0 分"
    # 必须单调不增：越久越低，且中间档要严格高于一个月以上
    assert P.ACT_SCORE["本周活跃"] > P.ACT_SCORE["半年前活跃"]
    assert P.ACT_SCORE["3日内活跃"] > P.ACT_SCORE["本周活跃"]


# ---------- 硬过滤 ----------

def test_hard_drop_catches_what_previously_leaked_into_p0():
    assert E.hard_drop(row(job_invalid=1)) == "已失效"
    assert E.hard_drop(row(job_status_desc="职位已关闭")) == "已关闭"
    assert E.hard_drop(row(city="成都")).startswith("非目标城市")
    assert E.hard_drop(row(recruiter_title="猎头顾问")) == "猎头发布"
    # 日薪按**数值**判，不按文案：salary_text 里从来没有「元/天」（全库 0 条）
    assert E.hard_drop(row(salary_min_k=0.2, salary_max_k=0.3)) == "日薪(实习)"
    assert E.hard_drop(row()) == ""


def test_cv_rule_compares_word_counts_instead_of_any_hit():
    """CV 判定看 **CV 词数 vs AI 词数**，不是"出现 CV 词即命中"。

    原先按 `any` 判，实测 48 条被拦的岗位里 27 条 AI 词多于 CV 词。
    最极端的 `AI工程师（视觉与大模型方向）` 是 cv=1 vs **ai=12** ——
    tech_req 里写满 LangChain/CrewAI/Function Calling/LoRA，是 YOLO+Agent
    联合系统岗，被单个"目标检测"一票否决。弱信号不该当门槛。
    """
    # AI 压倒性更多 → 放行（真实案例形态）
    joint = row(title="AI工程师（视觉与大模型方向）", glm_tags_json=json.dumps({
        "tech_req": ["目标检测", "LangChain、LlamaIndex、AutoGPT、CrewAI、Dify",
                     "Function Calling / Tool Call 机制，具备Agent架构设计能力",
                     "SFT/LoRA 微调", "RAG 知识库", "python", "大模型"]}))
    assert not E.is_cv_job(joint), "AI 词远多于 CV 词，不该判 CV 岗"
    # 纯 CV 工程 → 仍然拦（真实案例：OCR 岗，"大模型"只出现在 OCR大模型 里）
    ocr = row(title="初级算法工程师（计算机视觉+大模型）", glm_tags_json=json.dumps({
        "tech_req": ["精通numpy、OpenCV、PIL等主流图像处理工具库",
                     "熟悉计算机视觉、文本检测识别基础算法原理",
                     "熟练掌握Python编程语言", "OCR大模型等开源框架"]}))
    assert E.is_cv_job(ocr), "CV 词多于 AI 词，应判 CV 岗"


def test_aigc_title_is_not_hard_dropped_and_content_words_never_filter():
    """`AIGC` 不按标题硬删；且**不能**改用 JD 关键词计数来替代。

    第一段：AIGC 进 EXCLUDE_TITLE 时拦对 199 条内容/设计/运营岗，但同时吃掉
    22 条开发/算法/产品岗，且硬删发生在打标签之前 —— 那 22 条 LLM `match`
    全是 None，连"该不该降"都没判过。

    第二段（更重要，这是撤回过的方案）：改成按 JD 里"创意/剪辑/素材/分镜"
    等词计数判，实测 17 条被拦的里 **16 条是真 Agent 开发岗**，例如
    `多智能体应用研发工程师（AI 短剧 Agent）`——JD 写着"搭建编剧、导演、
    分镜、素材处理多 Agent 协作系统"。那些词描述的是 **Agent 要自动化的
    业务对象**，不是岗位要人做的事。岗位性质交给读过全文的 LLM 判
    （apply_glm_filters 按 job_type 降级），关键词只适合判标题直标的确定信号。
    """
    assert "AIGC" not in P.EXCLUDE_TITLE, "AIGC 不该按标题硬删"
    # 内容领域的 Agent 开发岗：JD 里必然出现剪辑/素材/分镜，绝不能因此被拦
    agent_dev = row(
        title="多智能体应用研发工程师（AI 短剧 Agent）",
        job_description="搭建编剧、导演、分镜、素材处理多 Agent 协作应用系统，"
                        "把剧本创作、分镜拆解、素材调度等业务流程自动化落地",
        glm_tags_json=json.dumps({"job_type": "开发岗", "match": "keep"}))
    assert E.hard_drop(agent_dev) == "", "内容领域的 Agent 开发岗不该被硬过滤"
    # 标题带 AIGC 的开发岗同样放行
    aigc_dev = row(title="AIGC agent应用工程师",
                   glm_tags_json=json.dumps({"job_type": "开发岗"}))
    assert E.hard_drop(aigc_dev) == ""
    # 硬过滤层不该有基于内容词计数的岗位性质判定
    assert not hasattr(P, "NONDEV_ROLE_KW"), \
        "撤回过的方案：内容词计数会误杀内容领域的 Agent 开发岗"


def test_hard_drop_also_catches_od_so_the_over_bucket_cannot_leak_it():
    """超纲桶直接从 sample 读、只过 hard_drop —— 它漏什么就会绕回页面。

    实测（2026-08-29）：`should_demote` 加了 OD 判定后，3 条华为 OD 岗
    仍从超纲页回到了页面，因为 hard_drop 这一层没查。
    两层规则集不对称本身就是漂移源。
    """
    assert E.hard_drop(row(title="AI agent/java开发工程师-OD招聘")) == "外包[OD]"
    # 词边界：Coding 不能被吃掉（这是 OD 规则的全部难点）
    assert E.hard_drop(row(title="AI Coding Agent 研发工程师")) == ""


def test_whitelisted_cities_pass_hard_drop():
    """杭州/上海/深圳都在投递池范围内；名单外的城市才剔除。"""
    for city in ("杭州", "上海", "深圳"):
        assert E.hard_drop(row(city=city)) == "", city
    assert P.city_score("杭州") == P.city_score("上海") == P.city_score("深圳") > P.city_score("成都")


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
