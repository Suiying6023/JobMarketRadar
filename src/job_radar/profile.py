"""求职画像单一真源：所有筛选阈值、名单、评分权重集中在此。

背景：阈值原先散落在 clean_local_db.py / export_pool_html.py /
analyze_local_filter.py / apply_glm_filters.py 四份文件里，已经开始漂移
（应届经验值三份写法、学历白名单缺项、公司黑名单缺 1 家）。
2026-08-10 收敛为本模块，各脚本一律 `from job_radar.profile import ...`。

用户画像：26 届应届本科双非，杭州 AI/Agent 开发岗。
"""
from __future__ import annotations

import re as _re
from datetime import date, timedelta

# ---------- 届数 ----------
COHORT_YEAR = 2026          # 用户毕业届；标题含更晚届数（27届+）即剔除

# ---------- 城市 ----------
# 投递池城市白名单。采集侧按全国抓（config 里配城市），这里决定**哪些进池**：
# 名单外的岗位降级为 sample —— 仍在库里参与增量去重，只是不进投递池。
# 想临时看全国盘子用环境变量，不必改代码：
#   set JOB_RADAR_CITIES=*              # 不限城市
#   set JOB_RADAR_CITIES=杭州,上海,深圳,苏州
import os as _os

_env_cities = (_os.environ.get("JOB_RADAR_CITIES") or "").strip()
if _env_cities == "*":
    CITY_WHITELIST: set[str] | None = None      # None = 不限城市（全国）
elif _env_cities:
    CITY_WHITELIST = {c.strip() for c in _env_cities.split(",") if c.strip()}
else:
    CITY_WHITELIST = {"杭州", "上海", "深圳"}

# 池内排序偏好：同等条件下这些城市优先展示（杭州本地 > 长三角 > 其他）。
# 与白名单是两件事——白名单决定「能不能进池」，这里决定「排多前」。
CITY_PREFERENCE = {"杭州": 1.0, "上海": 0.6, "深圳": 0.5}
CITY_PREFERENCE_DEFAULT = 0.0


def city_allowed(city: str) -> bool:
    """城市是否在投递池范围内。`CITY_WHITELIST is None` 表示全国不限。"""
    if CITY_WHITELIST is None:
        return True
    return not city or city in CITY_WHITELIST


def city_score(city: str) -> float:
    """城市排序加分。白名单只管准入，这个管池内优先级。"""
    return CITY_PREFERENCE.get(city or "", CITY_PREFERENCE_DEFAULT)

# ---------- 经验 / 学历 ----------
# 注意：这里是 **列表接口返回值**（DB 的 experience 字段），不是 BOSS 搜索参数码。
# 搜索参数写「应届生」，接口返回「在校/应届」——analyze_local_filter.py 曾用错值。
ALLOW_EXP = {"在校/应届", "经验不限", "1年以内", "1-3年"}
ALLOW_DEG = {"本科", "大专", "学历不限", "中专/中技", "高中", "初中及以下"}

# ---------- 薪资（单位 K，均按 12 薪折算后的月均口径比较） ----------
# 门槛按**薪资区间均值**判，不按起薪：起薪只是区间下沿，平台标的范围是给所有
# 候选人的，卡起薪会把「12-30K」这类实际可投的岗位一并剔掉。
SALARY_DROP_MID_ABOVE = 25.0   # 区间均值超此值 → 超出可达范围，降级
SALARY_LOW_MID_MAX = 7.0       # 中点 ≤ 此值 → 低薪页单列
SALARY_HIGH_MID_MIN = 15.0     # 中点 > 此值 → 高薪页单列
DAILY_MARKERS = ("元/天", "元/日")   # 日薪 = 实习岗

# ---------- 活跃度分档（天） ----------
ACTIVE_P0_MAX_DAYS = 3         # ≤3 天 → P0 优先；None（平台未给）→ P1 待确认；其余 → P2 冷池

# ---------- 公司风险 ----------
RISK_MIN_CAPITAL_WAN = 100.0   # 注册资本 < 100 万 → 低资本
RISK_MIN_AGE_MONTHS = 12       # 成立不足 12 个月 → 新成立
# 非公司主体：个体工商户/个人独资/合伙 —— 抗风险能力与有限责任公司不可比
RISK_ENTITY_MARKS = ("个体", "个人独资", "合伙")

# ---------- 标题 / 公司黑名单 ----------
EXCLUDE_TITLE = (
    "销售", "主播", "客服", "电话", "猎头", "劳务派遣",
    "外包", "外派", "驻场",     # 2026-08-04 抽查：标题直标外包的岗
    "实习",                      # 用户要正式岗
    "AIGC",                      # 2026-08-05：词太宽，带出纯设计/内容岗
)
EXCLUDE_COMPANY = (
    "猎头", "劳务派遣", "人力资源",
    # IT/人力外包服务公司（名称关键词，命中即降级；2026-08-03 确认）
    "外企德科", "万宝盛华", "软通动力", "博彦科技", "易宝软件",
    "紫光云数", "中软国际", "文思海辉", "中电金信", "法本信息",
    "东软", "凯捷", "汉得", "新致软件", "华钦", "勇冠睿智",
)
# 招聘者头衔含此类词 = 猎头/中介发布，比按公司名判断可靠
EXCLUDE_RECRUITER_TITLE = ("猎头", "顾问", "中介")
# 匿名公司名兜底正则，只在 raw.anonymous 不可用时使用（见 is_anonymous）。
# 形态取自实测：库里 128 个不同的匿名公司名，「某」只出现在位置 0（「某知名互联网公司」）
# 或位置 2（城市前缀恰好两字，「杭州某中型人工智能公司」），没有第三种。
# 不用裸「某」子串：那会误伤「杭州果集某科技」这类真实公司名。
_ANON_NAME_RE = _re.compile(r"^(?:[一-龥]{2})?某")

# ---------- CV / 图像算法（用户不投） ----------
CV_TITLE_KW = (
    "视觉算法", "图像算法", "CV算法", "机器视觉", "计算机视觉", "图像处理",
    "图像识别", "3D视觉", "图像分割", "视频算法",
)
# 具体技能词：出现即=要求。裸「cv」不放这里——它在「NLP/CV/模型压缩」这类
# 备选列表里只是可选项，会误杀 LLM 研究员岗，单独精确匹配处理。
CV_TECH_KW = (
    "计算机视觉", "图像处理", "图像识别", "目标检测", "图像分割", "opencv",
    "点云", "3d视觉", "机器视觉", "图像算法", "视觉算法", "视频算法",
)

# ---------- AI 技术栈（评分用，子串匹配） ----------
AI_TECH = (
    "python", "llm", "大模型", "agent", "rag", "pytorch", "向量", "langgraph",
    "fastapi", "function calling", "aigc", "深度学习", "机器学习", "微调",
    "prompt", "多模态", "openai", "langchain", "transformer", "智能体",
    "mcp", "dify", "coze", "vllm", "milvus", "faiss", "embedding",
)

# ---------- 弱负分信号 ----------
# 「底薪加提成」多为公司统一勾选的福利模板：实测 67 条里含浙大/大厂，
# JD 无销售内容者居多。因此**不硬过滤**，只作弱负分。
SALES_WELFARE_MARKS = ("底薪加提成", "保底工资")
OVERTIME_WELFARE_MARKS = ("加班补助", "节假日加班费", "夜班补助")

# ---------- 评分权重 ----------
# 设计原则：活跃度已用于 P0/P1/P2 分页，页内不该再占最大权重（否则同质维度
# 压过技术栈匹配）。技术栈匹配是唯一反映「岗位对不对口」的项，给最高权重。
W_TECH_MAX = 3.0          # 技术栈命中数 × 0.5，上限 3.0
W_SKILLS_MAX = 1.5        # BOSS 结构化 skills_json 交集，× 0.5，上限 1.5
W_ACT_MAX = 1.0           # 页内活跃度微调
W_SALARY_MAX = 1.5        # 薪资在档内的相对位置
W_EXP_FRIENDLY = 1.0      # 经验不限 / 在校应届 → 应届生友好
W_CITY_MAX = 1.0          # 城市偏好（见 CITY_PREFERENCE），× city_score()
W_SALES_PENALTY = -0.8    # 销售性质弱信号
W_UNCERT_PENALTY = -0.5   # 招聘者未认证

# ---------- 数据时效 ----------
# 活跃度/岗位状态都是抓详情那一刻的快照，不随时间衰减：一条 30 天前记录的
# 「刚刚活跃」今天毫无参考价值，但它照旧按「刚刚活跃」参与排序并占据榜首。
# 处理分两层，见 freshness() 与 stale_penalty()：
#   ① 快照类信号（活跃度）按 freshness() 衰减 —— 过期即退化成无信息（0 分）
#   ② 另加一笔小额扣分，代表「可能已下架」的风险
# ⚠️ 别只用 ② 而把 ① 留成满分：那样过期的「在线」要靠扣分去抵，而扣分对
# 技术栈匹配这类与时间无关的项同样生效，会把对口岗位一起压下去。
STALE_FRESH_DAYS = 7       # ≤ 此天数视为新鲜，快照可信
STALE_FULL_DAYS = 30       # ≥ 此天数快照按无信息处理
# 量级刻意小于 W_ACT_MAX：它只代表下架风险，活跃度过期由 freshness() 处理。
W_STALE_PENALTY = -0.6

SIZE_SCORE = {
    # 公司规模不作为偏好维度：只保留极小公司的轻微扣分（人数过少通常意味着
    # 岗位职责与稳定性不确定），其余规模一律同分，不替用户预设偏好。
    "0-20人": 0.8,
}
SIZE_DEFAULT = 1.0

STAGE_SCORE = {
    "已上市": 1.5, "A轮": 1.5, "B轮": 1.5, "C轮": 1.5, "D轮及以上": 1.5,
    "天使轮": 1.0, "不需要融资": 1.0, "未融资": 0.8,
}
STAGE_DEFAULT = 1.0    # 缺失值不等于最差值（原先与「未融资」同分 0.8）

ACT_SCORE = {"在线": 1.0, "刚刚活跃": 0.9, "今日活跃": 0.7, "3日内活跃": 0.4}


def risk_cutoff_date(today: date | None = None) -> str:
    """「新成立」判定日期界（相对今天推算，取代写死的 '2025-08-09'）。

    写死日期会静默失准：2026-11 再跑时 '2025-08-09' 变成「距今 15 个月」。
    """
    today = today or date.today()
    return (today - timedelta(days=RISK_MIN_AGE_MONTHS * 30)).isoformat()


def is_daily_salary(salary_text: str) -> bool:
    return any(m in (salary_text or "") for m in DAILY_MARKERS)


def salary_mid(min_k, max_k, months=None) -> float | None:
    """薪资区间均值（K/月），按 12 薪口径折算。缺值返回 None。

    库里 183 条带 ·13薪~·16薪。`15-25K·15薪` 的实际年薪高于 `18-28K·12薪`，
    忽略月数会把前者当低薪。分档、排序、门槛判定必须共用这一个口径。
    """
    if min_k is None or max_k is None:
        return None
    return (min_k + max_k) / 2 * (months or 12) / 12


def salary_out_of_range(min_k, max_k, months=None) -> bool:
    """薪资是否超出可达范围（按区间均值判，不按起薪）。"""
    mid = salary_mid(min_k, max_k, months)
    return mid is not None and mid > SALARY_DROP_MID_ABOVE


def freshness(days_ago: int | None) -> float:
    """快照可信度，1.0（新鲜）→ 0.0（过期）。`days_ago` 是观测距今天数。

    用途是给**快照类信号**（活跃度、岗位状态）加权：这些字段记的是抓详情
    那一刻的瞬时状态，不随时间衰减地留在库里。实测 2668 条 active 标着
    「在线」，其中大多数快照是 19 天前采的 —— 那时的「在线」今天既不能证明
    在线也不能证明不在线，它的信息量是 0，不是负数。

    None（从未观测）返回 0.0：同样是无信息，不是坏消息。
    """
    if days_ago is None:
        return 0.0
    if days_ago <= STALE_FRESH_DAYS:
        return 1.0
    if days_ago >= STALE_FULL_DAYS:
        return 0.0
    span = STALE_FULL_DAYS - STALE_FRESH_DAYS
    return 1.0 - (days_ago - STALE_FRESH_DAYS) / span


def stale_penalty(days_ago: int | None) -> float:
    """久未核实的额外扣分，独立于 `freshness()` 的信号衰减。

    两者分工不同，都需要：`freshness()` 让过期的活跃度**退化成无信息**，
    而这里扣的是「这条可能已经下架了」的风险 —— 与它当初活跃度多高无关。
    所以量级要明显小于 W_ACT_MAX，否则会连带惩罚技术栈匹配这类
    **与时间无关**的判断（对口就是对口，不会因为记录旧了就变得不对口）。
    """
    return W_STALE_PENALTY * (1.0 - freshness(days_ago))


def is_anonymous(raw: dict | None, company_name: str = "") -> bool:
    """公司身份是否被 BOSS 匿名化（投递前无法核实，一律不进池）。

    判据是列表接口的结构化字段 `raw.anonymous`：非 0 即匿名。
    实测 2964 条里 342 条匿名，`anonymous` 与「公司名含某」交叉一致 341 条，
    且匿名组 `brand_id` 100% 为空（正常组仅 10% 空）。
    结构化字段还多抓到 1 条名字里没有「某」的（`知名互联网公司`），
    而纯字符串匹配零误杀但会漏这类 —— 所以以字段为主，字符串仅作兜底
    （老库 raw_json 可能已被列表轮次覆盖，那时只剩名字可依据）。
    取值本身无语义（实测见到 1/3/4/8/9/12/20/21/100/101），不要按数值分类。
    """
    if raw:
        try:
            if int(raw.get("anonymous") or 0) != 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(_ANON_NAME_RE.match((company_name or "").strip()))


def match_keyword(text: str, keywords) -> str | None:
    """返回第一个命中的关键词，未命中返回 None。

    原 clean_local_db.py 写成 `any(kw in t for kw in KW)` 后又引用 `kw`，
    生成器作用域不泄漏 → NameError，那两个分支从未真正生效过。
    """
    low = (text or "").lower()
    return next((k for k in keywords if k.lower() in low), None)
