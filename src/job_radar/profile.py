"""求职画像单一真源：所有筛选阈值、名单、评分权重集中在此。

背景：阈值原先散落在 clean_local_db.py / export_pool_html.py /
analyze_local_filter.py / apply_glm_filters.py 四份文件里，已经开始漂移
（应届经验值三份写法、学历白名单缺项、公司黑名单缺 1 家）。
2026-08-10 收敛为本模块，各脚本一律 `from job_radar.profile import ...`。

用户画像：26 届应届本科双非，杭州 AI/Agent 开发岗。
"""
from __future__ import annotations

from datetime import date, timedelta

# ---------- 届数 ----------
COHORT_YEAR = 2026          # 用户毕业届；标题含更晚届数（27届+）即剔除

# ---------- 城市 ----------
# 目标城市。**扩城市时必须改这里**，否则新抓的外地岗会被立刻判成"非目标城市"：
# `clean_local_db` 把它降级成 sample、`export_pool_html.hard_drop` 直接剔除，
# 等于白花配额抓回来又全部丢掉（2026-08-11 扩上海/深圳前踩到）。
#
# 可用环境变量覆盖，不用改代码：
#   set JOB_RADAR_CITIES=杭州,上海,深圳
# 留空则用默认值。全国筛选时设成空字符串特殊值 `*` 表示不限城市。
import os as _os

_env_cities = (_os.environ.get("JOB_RADAR_CITIES") or "").strip()
if _env_cities == "*":
    CITY_WHITELIST: set[str] | None = None      # None = 不限城市（全国）
elif _env_cities:
    CITY_WHITELIST = {c.strip() for c in _env_cities.split(",") if c.strip()}
else:
    CITY_WHITELIST = {"杭州"}


def city_allowed(city: str) -> bool:
    """城市是否在目标范围内。`CITY_WHITELIST is None` 表示全国不限。"""
    if CITY_WHITELIST is None:
        return True
    return not city or city in CITY_WHITELIST

# ---------- 经验 / 学历 ----------
# 注意：这里是 **列表接口返回值**（DB 的 experience 字段），不是 BOSS 搜索参数码。
# 搜索参数写「应届生」，接口返回「在校/应届」——analyze_local_filter.py 曾用错值。
ALLOW_EXP = {"在校/应届", "经验不限", "1年以内", "1-3年"}
ALLOW_DEG = {"本科", "大专", "学历不限", "中专/中技", "高中", "初中及以下"}

# ---------- 薪资（单位 K，均按 12 薪折算后的月均口径比较） ----------
SALARY_DROP_MIN_ABOVE = 20.0   # 起薪超此值 → 自认实力不够，去除
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
ANON_MARK = "某"    # BOSS 隐藏公司身份（「某大型XX公司」）—— 投递前无法核实，一律剔除

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
W_SALES_PENALTY = -0.8    # 销售性质弱信号
W_UNCERT_PENALTY = -0.5   # 招聘者未认证

SIZE_SCORE = {
    # 中型公司(100-999)给最高分：技术岗成长性好且对双非应届门槛友好；
    # 大厂(1万+)给 1.5 是因为双非应届通过率低，不是说大厂不好。
    "100-499人": 2.0, "500-999人": 2.0, "1000-9999人": 1.5, "10000人以上": 1.5,
    "20-99人": 1.0, "0-20人": 0.5,
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


def match_keyword(text: str, keywords) -> str | None:
    """返回第一个命中的关键词，未命中返回 None。

    原 clean_local_db.py 写成 `any(kw in t for kw in KW)` 后又引用 `kw`，
    生成器作用域不泄漏 → NameError，那两个分支从未真正生效过。
    """
    low = (text or "").lower()
    return next((k for k in keywords if k.lower() in low), None)
