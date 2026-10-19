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
from pathlib import Path as _Path

# ---------- 城市 ----------
#
# 2026-10-19 删除了 `CITY_WHITELIST` / `city_allowed()` / `JOB_RADAR_CITIES`：
# 它们**没有任何调用点**了（消费它的 `clean_local_db` / `export_pool_html` 已删），
# 而且和 `CandidateProfile.cities` 是**两套相反语义** —— 这边缺省是白名单
# {杭州,上海,深圳}，那边空列表 = 不限城市。两套并存时 GUI 的城市筛选与导出准入
# 会互相打架（2026-09-13 的审查记录过这个冲突）。城市的唯一真源现在是
# `config.local.yaml` 的 `candidate.cities`。
#
# 池内排序偏好保留：它决定「排多前」，不是「能不能进池」。
CITY_PREFERENCE = {"杭州": 1.0, "上海": 1.0, "深圳": 1.0}
CITY_PREFERENCE_DEFAULT = 0.0


def city_score(city: str) -> float:
    """城市排序加分。白名单只管准入，这个管池内优先级。"""
    return CITY_PREFERENCE.get(city or "", CITY_PREFERENCE_DEFAULT)

# ---------- 经验 / 学历 ----------
# 注意：这里是 **列表接口返回值**（DB 的 experience 字段），不是 BOSS 搜索参数码。
# 搜索参数写「应届生」，接口返回「在校/应届」——analyze_local_filter.py 曾用错值。
# ⚠️ 但接口**有时也直接返回「应届生」**（2026-08-29 实测库里有 1 条）。
# 漏掉它的后果是语义完全反了：「应届生」恰恰是最该保留的值，却被判「经验不符」降级。
# 这类白名单漏值不会报错，只会安静地降掉最对口的岗位。
ALLOW_EXP = {"在校/应届", "应届生", "经验不限", "1年以内", "1-3年"}
ALLOW_DEG = {"本科", "大专", "学历不限", "中专/中技", "高中", "初中及以下"}

# ---------- 薪资（单位 K） ----------
# 门槛按**薪资区间均值**判，不按起薪：起薪只是区间下沿，平台标的范围是给所有
# 候选人的，卡起薪会把「12-30K」这类实际可投的岗位一并剔掉。
#
# ⚠️ **门槛与排序用不同口径，这是刻意的**（2026-08-29 修）：
# · 排序/分桶用 12 薪折算（`salary_mid`）—— `15-25K·15薪` 的年薪确实高于
#   `18-28K·12薪`，忽略月数会把前者当低薪。
# · **门槛用月基薪**（`salary_raw_mid`）—— "够不着"看的是月薪能不能谈到，
#   年终奖倍数不改变门槛。
# 混用的后果实测过：折算把 **475 条** raw≤25 的推过 25 门槛、**反向 0 条**
# （单向抬升），其中 267 条"超纲"是唯一降级理由。典型是 `15-30K·14薪`
# （raw 22.5 → 26.2）和 `12-15K·24薪`（raw 13.5 → 27.0）—— 月基薪 12-18K
# 的岗位因为年终奖厚被判"超出可达范围"，而标题里明写着"26届可投""经验不限"。
SALARY_DROP_MID_ABOVE = 25.0   # **月基薪**区间均值超此值 → 超出可达范围，降级
SALARY_LOW_MID_MAX = 7.0       # 中点 ≤ 此值 → 低薪页单列（12 薪折算口径）
SALARY_HIGH_MID_MIN = 15.0     # 中点 > 此值 → 高薪页单列（12 薪折算口径）

# ---------- 活跃度分档（天） ----------
ACTIVE_P0_MAX_DAYS = 3         # ≤3 天 → P0 优先；None（平台未给）→ P1 待确认；其余 → P2 冷池

# ---------- 公司风险 ----------
#: 注册资本下限（万元）。低于这条线判「低资本」。
#:
#: 为什么卡 100 万：2026-10-19 查「深圳市知行远创科技」发现注册资本只有 50 万，
#: 而它在今天的优先清单里排第一（99 分）。50 万注册资本的小公司承担不起一个
#: 正式员工的用工风险 —— 拖欠工资、随时解散都是现实风险。100 万是本地一家
#: 正常软件公司的下限量级。
RISK_MIN_CAPITAL_WAN = 100.0
#: 成立不足这么多个月判「新成立」。12 个月是能完整走过一个财年的最短周期。
RISK_MIN_AGE_MONTHS = 12
# 非公司主体：个体工商户/个人独资/合伙 —— 抗风险能力与有限责任公司不可比
RISK_ENTITY_MARKS = ("个体", "个人独资", "合伙")


def _parse_capital_wan(raw: str) -> float | None:
    """把「100万人民币」「1644.7496万元」「5000万美元」解析成万元。

    外币不做汇率换算 —— 直接返回 None，宁可漏判也不要用猜测的汇率去筛公司。
    """
    text = (raw or "").strip()
    if not text:
        return None
    # 外币（美元/港元/欧元…）一律放弃判断
    if any(mark in text for mark in ("美元", "港元", "欧元", "日元", "英镑", "新台币")):
        return None
    match = _re.search(r"([\d.]+)\s*万", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    match = _re.search(r"([\d.]+)\s*亿", text)
    if match:
        try:
            return float(match.group(1)) * 10000
        except ValueError:
            return None
    match = _re.search(r"([\d,]+)\s*元", text)
    if match:
        try:
            return float(match.group(1).replace(",", "")) / 10000
        except ValueError:
            return None
    return None


def company_risk(reg: dict) -> dict:
    """按工商登记字段判断公司规模风险。`reg` 是 `company_reg_json` 解析后的字典。

    返回 `{"level": "low"|"new"|"noncorp"|"ok"|"unknown", "reasons": [...]}`。
    没有工商数据时返回 `unknown` —— **不把「没数据」当成「有风险」**，
    否则覆盖只有 4% 时会把 96% 的岗位全判成风险。
    """
    reasons: list[str] = []
    if not reg:
        return {"level": "unknown", "reasons": []}

    capital = _parse_capital_wan(reg.get("注册资本") or "")
    if capital is not None and capital < RISK_MIN_CAPITAL_WAN:
        reasons.append(f"注册资本 {capital:g} 万，低于 {RISK_MIN_CAPITAL_WAN:g} 万")

    entity = (reg.get("企业类型") or "") + (reg.get("企业名称") or "")
    if any(mark in entity for mark in RISK_ENTITY_MARKS):
        reasons.append("非公司主体（个体/个人独资/合伙）")

    founded = (reg.get("成立时间") or "")[:10]
    if founded:
        try:
            age_days = (date.today() - date.fromisoformat(founded)).days
            if age_days < RISK_MIN_AGE_MONTHS * 30:
                reasons.append(f"成立仅 {age_days // 30} 个月")
        except ValueError:
            pass

    if not reasons:
        return {"level": "ok", "reasons": []}
    # 低资本是最硬的一条，其余算附带说明。
    level = "low" if capital is not None and capital < RISK_MIN_CAPITAL_WAN else "new"
    return {"level": level, "reasons": reasons}


# ---------- 标题 / 公司黑名单 ----------
EXCLUDE_TITLE = (
    "销售", "主播", "客服", "电话", "猎头", "劳务派遣",
    "外包", "外派", "驻场",     # 2026-08-04 抽查：标题直标外包的岗
    "实习",                      # 用户要正式岗
)
# ⚠️ `AIGC` 曾在此表（2026-08-05 加，理由"词太宽，带出纯设计/内容岗"），
# 2026-08-29 移出：它拦对了 199 条内容/设计/运营岗，但**同时吃掉 22 条
# 开发/算法/产品岗**（`AIGC agent应用工程师` 16-28K、`AIGC 视频工作流工程师`），
# 而且硬删发生在打标签之前 —— 那 22 条的 LLM `match` 全是 None，
# 连"该不该降"都没经过判定。
# 改由 `export_pool_html.is_nondev_role()` 按 JD 内容判，见 NONDEV_ROLE_KW。
# 同一形态的错误此前在 `OD`（吃掉 Coding）上踩过一次。

# 「OD」是华为体系的外包/外派用工标记（`数据库内核开发工程师（OD）`、
# `【od】ai软件开发-python`），性质等同上面的「外包/外派」。
#
# ⚠️ **必须按词边界匹配，不能进 EXCLUDE_TITLE**（那里走裸子串）：
# 实测（2026-08-29）裸子串 `OD` 在库里命中 91 条，其中绝大多数是 **Coding**
# —— `AI Coding Agent 研发工程师`、`Vibe Coding 全栈工程师`、
# `淘天-AI Agent应用开发工程师-coding方向` …… 那恰恰是求职者最对口的方向。
# 按词边界则精准命中 20 条，全是华为 OD 岗。
# 这是「短英文缩写做黑名单」的典型陷阱：词越短，越可能是别的词的一部分。
#
# 否定形式要放行：`华为社招-数据库内核研发-非od` 明确说了不是 OD。
_OD_RE = _re.compile(r"(?<![A-Za-z])OD(?![A-Za-z])", _re.I)
_NOT_OD_RE = _re.compile(r"非\s*OD", _re.I)


def outsourced_od(title: str) -> bool:
    """标题是否带华为 OD（外包用工）标记。"""
    t = title or ""
    if _NOT_OD_RE.search(t):
        return False
    return bool(_OD_RE.search(t))


# ---------- 外包 / 人力服务公司名单 ----------
#
# 为什么需要名单：**JD 正文里几乎不写外包**。2026-10-19 实测 3,072 条在招且有
# 正文的岗位里，明写「劳务派遣 / 用工性质 / 与人力公司签约」的只有 1 条，
# `外派至` 0 条。不写的东西任何文本方法都找不到（LLM 也一样），所以识别只能
# 靠**公司主体身份**。
#
# 反过来，往 JD 里加关键词正则是**更糟**的方案：JD 里 `外包|外派|驻场|客户现场`
# 命中 124 条，其中 17 条是在主动声明自己**不是**外包（「非外包项目！」「自研产品…
# 非外包性质」），其余多为实施/交付/售前/售后岗 —— `客户现场`、`驻场` 在中文
# 技术 JD 里绝大多数指**工作地点**而非用工主体。精确率约 1–2%。
#
# 匹配规则由名单文件头定义，这里只实现：
#   · 强词（≥4 汉字 或 ≥6 拉丁）→ 允许子串
#   · 短词（<4 汉字 且 <6 拉丁）→ 只允许全等，或去掉 2~4 字省市前缀后以前缀开头
# 后者是为了挡掉 `华秋智联`（撞「智联」）、`海宇信息`（撞「宇信」）、
# `山东软件科技`（撞「东软」）这类误报。
STAFFING_PATH = _Path(__file__).with_name("data") / "staffing_companies.txt"

_PREFIX_WORDS = (
    "北京", "上海", "天津", "重庆", "深圳", "杭州", "广州", "南京", "苏州", "武汉",
    "成都", "西安", "厦门", "青岛", "大连", "宁波", "合肥", "长沙", "郑州", "济南",
    "福州", "昆明", "南昌", "贵阳", "太原", "沈阳", "长春", "哈尔滨", "兰州", "银川",
    "西宁", "南宁", "海口", "石家庄", "乌鲁木齐", "呼和浩特",
    "浙江", "江苏", "广东", "山东", "河南", "河北", "湖南", "湖北", "安徽", "福建",
    "江西", "四川", "辽宁", "吉林", "陕西", "山西", "云南", "贵州", "广西", "海南",
    "甘肃", "青海", "宁夏", "新疆", "西藏", "内蒙古", "黑龙江", "香港", "澳门", "台湾",
)


def _is_strong(term: str) -> bool:
    """强词：汉字 ≥4 个，或拉丁字符 ≥6 个。"""
    cjk = sum(1 for ch in term if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 4 or (cjk == 0 and len(term) >= 6)


def _load_staffing() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """读名单，返回 (强词, 短词)。文件缺失时返回空 —— 数据文件不该让采集起不来。"""
    try:
        lines = STAFFING_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return (), ()
    strong: list[str] = []
    short: list[str] = []
    for line in lines:
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        (strong if _is_strong(term) else short).append(term)
    return tuple(strong), tuple(short)


_STAFFING_STRONG, _STAFFING_SHORT = _load_staffing()


def is_staffing_company(name: str) -> bool:
    """公司名是否属于外包 / 人力派遣 / 人力服务商。

    短词（如 `中智`、`佰钧成`）**不做子串匹配** —— 2~3 字中文主体名做子串
    一定误报（实测踩过 智联/宇信/东软/微创/海隆/宝信 六个坑）。
    """
    text = (name or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if any(term.casefold() in lowered for term in _STAFFING_STRONG):
        return True
    for term in _STAFFING_SHORT:
        # ① 全等
        if text == term:
            return True
        # ② 本身就以前缀开头（`人瑞网络科技` ← `人瑞`、`中智科创` ← `中智`）。
        #    这一条不能省：去掉省市前缀是**补充**手段，不是前提条件。
        if text.startswith(term):
            return True
        # ③ 去掉 2~4 字省市前缀后以该词开头（`深圳中智`、`武汉佰钧成`）。
        for prefix in _PREFIX_WORDS:
            if text.startswith(prefix) and text[len(prefix):].startswith(term):
                return True
    return False


def staffing_signal(row: dict) -> str:
    """返回外包判定依据（空串表示没命中）。三路信号并联，取第一个命中的。

    名单、`industry` 行业标签、平台代招标记三者**正交**：
      · 名单抓得到 ITO 大厂（它们的行业标「计算机软件」，不是「人力资源服务」）
      · `industry='人力资源服务'` 抓得到名单没收的小微/区域人力公司
      · `is_proxy_job` 是平台自己标的代招
    2026-10-19 实测三路并集 944 条 / 在招 236 条，单看任何一路都少一截。
    """
    if is_staffing_company(row.get("company_name") or ""):
        return "公司名在人力服务名单内"
    if (row.get("industry") or "").strip() == "人力资源服务":
        return "行业标为人力资源服务"
    if row.get("is_proxy_job"):
        return "平台标记为代招"
    return ""


# 2026-10-19 删除 `future_cohort()` / `_COHORT_RE` / `COHORT_YEAR`：
# 零调用点，且已被 `analysis/decisions.py::evaluate` 的届别判定取代（那里覆盖得
# 更全：`20XX届`、`26届`、`2026年应届`、以及 `26/27届` 这种区间写法）。
# 原实现里那条「届数只存在于标题、LLM 从正文读不出来」的教训已移到 decisions.py。
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
# 具体技能词。⚠️ **不是"出现即命中"**：见 is_cv_job —— 要和 AI/Agent 词比数量。
# 裸「cv」不放这里——它在「NLP/CV/模型压缩」这类备选列表里只是可选项。
CV_TECH_KW = (
    "计算机视觉", "图像处理", "图像识别", "目标检测", "图像分割", "opencv",
    "点云", "3d视觉", "机器视觉", "图像算法", "视觉算法", "视频算法",
)

# ---------- 设计 / 美术 / 内容运营岗 ----------
# **这里刻意没有关键词表**。判定交给 LLM 的 `job_type`
# （在 apply_glm_filters.classify 里降级设计岗/内容岗/运营岗/市场岗）。
#
# 2026-08-29 试过建一份关键词表按 JD 命中数判，**实测失败并撤回**：
# 17 条被拦的里 16 条是真 Agent/大模型开发岗，例如
# `多智能体应用研发工程师（AI 短剧 Agent）`（JD 写"搭建编剧、导演、分镜、
# 素材处理多 Agent 协作系统"）、`Agent 应用工程师（兴趣电商方向）`。
# 根因：**那些词描述的是 Agent 要自动化的业务对象，不是岗位要人做的事**。
#
# 更值得记住的是筛词方法本身错了：我用"标题含 AIGC 的 233 条"分组算文档频率，
# 得到创意 +40pt、剪辑 +36pt 这些看着很强的区分度 —— 但"内容领域的 Agent
# 开发岗"在那个样本里几乎不存在。**筛判据的样本必须覆盖判据将要作用的全体**，
# 否则区分度是假的，而且假在你看不见的那一类上。
# （同源教训：`设计` 一词在两组分别是 61%/68%，开发组反而更高。）

# ---------- AI 技术栈（评分用，子串匹配） ----------
# 只加分，从不当门槛（弱信号只配排序）。
#
# ⚠️ 子串匹配有个反直觉的陷阱：**`大模型` 不是 `大语言模型` 的子串**
# （"大语言模型"里"大"和"模型"不相连）。实测 24 条 JD 只写「大语言模型」
# 从不写「大模型」，它们的技术栈分因此为 0。同类的中英文别名都要显式列出。
#
# 2026-08-29 按 JD 文档频率补漏词（n=827 active+有JD）：
#   工作流 21.2%、知识库 19.1%、claude 18.7%、gpt 12.2%、cursor 11.6%、
#   工具调用 9.8%、codex 9.4%、大语言模型 7.6%、llamaindex 7.0%、提示词 6.0%
# 其中 claude/cursor/codex 是 AI Coding 方向的核心标识，而那恰是求职者最对口
# 的方向 —— 漏掉它们让 `AI产品开发-Claude code`（tech_req 明写这三个词）
# 排到全池倒数第二。同一个方向此前已因「OD 黑名单误吃 Coding」踩过一次。
AI_TECH = (
    "python", "llm", "大模型", "大语言模型", "agent", "rag", "pytorch",
    "向量", "langgraph", "fastapi", "function calling", "工具调用",
    "深度学习", "机器学习", "微调", "prompt", "提示词", "多模态",
    "openai", "langchain", "llamaindex", "transformer", "智能体",
    "mcp", "dify", "coze", "vllm", "milvus", "faiss", "embedding",
    # Agent 工程的核心概念（中文表述在 JD 里比英文更常见）
    "工作流", "知识库",
    # AI Coding：这一族是求职者最对口的方向
    "claude", "cursor", "codex", "copilot",
    # 主流模型与框架
    "gpt", "deepseek", "qwen", "autogen", "crewai", "n8n",
)
# ⚠️ `aigc` 曾在此表，2026-08-29 移出：它同时在 EXCLUDE_TITLE 里被硬过滤，
# 一个词既加分又硬删是自相矛盾的（且 is_cv_job 的注释还写着"图像生成(AIGC)
# 不算图像算法"——三处判断打架）。它在 tech_blob 只命中 7 条，移出无实际损失。

# ---------- 弱负分信号 ----------
# 「底薪加提成」多为公司统一勾选的福利模板：实测 67 条里含浙大/大厂，
# JD 无销售内容者居多。因此**不硬过滤**，只作弱负分。
#
# ⚠️ 2026-08-29 复核后**取消了它的权重**（常量保留，供分析脚本用）：
# 388 条带标记的岗位 demote 率 63.1% vs 基线 53.4%，偏差 **+9.8pt**。
# 按本项目自己定的判据（最大偏差不足 16pt 视为无区分度）够不上权重 ——
# 而代价是实测让这 193 条平均下沉 82 个排名位，受害者包括
# `大模型算法工程师`（9.4 分，纯算法岗）、`AI Agent 算法工程师（应届毕业生）`。
# 弱信号只配排序不配惩罚：它勾没勾这个福利模板，和岗位是不是销售岗关系很弱
# （吃罚的岗位平均勾了 11.7 项福利，干净的只勾 6.0 项 —— 它测的是"福利勾得多"）。
SALES_WELFARE_MARKS = ("底薪加提成", "保底工资")

# ---------- 评分权重 ----------
# 设计原则：活跃度已用于 P0/P1/P2 分页，页内不该再占最大权重（否则同质维度
# 压过技术栈匹配）。技术栈匹配是唯一反映「岗位对不对口」的项，给最高权重。
W_TECH_MAX = 3.0          # 技术栈命中数 × 0.5，上限 3.0
W_SKILLS_MAX = 1.5        # BOSS 结构化 skills_json 交集，× 0.5，上限 1.5
W_ACT_MAX = 1.0           # 页内活跃度微调
W_SALARY_MAX = 1.5        # 薪资在档内的相对位置
W_EXP_FRIENDLY = 1.0      # 经验不限 / 在校应届 → 应届生友好
W_CITY_MAX = 1.0          # 城市偏好（见 CITY_PREFERENCE），× city_score()
# 以下两项 2026-08-29 归零：实测都够不上权重，而扣分会实际改变排名。
# 保留常量（而非删除）是为了让"曾经评估过并否决"这件事留在代码里 ——
# 直接删掉的话，下一个人会以为这两个信号从没被考虑过，很可能重新加回来。
W_SALES_PENALTY = 0.0     # 原 -0.8。见 SALES_WELFARE_MARKS 注释：偏差仅 +9.8pt
W_UNCERT_PENALTY = 0.0    # 原 -0.5。827 条 active+有JD 里只有 6 条未认证，无信号

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

# ⚠️ 必须覆盖**全部**文案档位，缺档一律得 0 分 —— 那等于让「本周活跃」和
# 「半年前活跃」完全等价。2026-08-29 实测 active+有 JD 的 827 条里
# **263 条（31.8%）的文案不在表里**：本周活跃 41、2周内 38、2月内 37、
# 本月 36、半年前 56、3-5月内 35、空 12。
# 这是 export_pool_html 顶部注释记过的「中间档落进裂缝」那个 bug 的**打分侧残留**
# —— 分页早已改按 recruiter_active_days 天数判，打分还在按文案白名单查表。
# 缺档不报错，只是安静地把一批岗位的活跃度信号清零。
ACT_SCORE = {
    "在线": 1.0, "刚刚活跃": 0.9, "今日活跃": 0.7, "3日内活跃": 0.4,
    # 中间档：仍有信号但明显弱于 3 日内，给递减的小分
    "本周活跃": 0.3, "2周内活跃": 0.2, "本月活跃": 0.1,
    # 一个月以上等同无信号（0.0），显式列出以示"已判过"而非"漏了"
    "2月内活跃": 0.0, "3月内活跃": 0.0, "4月内活跃": 0.0, "5月内活跃": 0.0,
    "近半年活跃": 0.0, "半年前活跃": 0.0,
}


def risk_cutoff_date(today: date | None = None) -> str:
    """「新成立」判定日期界（相对今天推算，取代写死的 '2025-08-09'）。

    写死日期会静默失准：2026-11 再跑时 '2025-08-09' 变成「距今 15 个月」。
    """
    today = today or date.today()
    return (today - timedelta(days=RISK_MIN_AGE_MONTHS * 30)).isoformat()


def is_daily_salary(min_k, max_k) -> bool:
    """是否日薪（实习岗）。

    ⚠️ 原先靠 `salary_text` 里的「元/天」「元/日」判，**全库命中 0 条**：
    `salary_text` 只有 `N-NK` 和 `N-NK·N薪` 两种形态，没有一条含天或日
    （2026-08-29 实测 8109 条）。那是一段死代码，而且它写在两层里，
    看起来像"有在防"实则从未生效。
    日薪岗在这个库里是以 `3-4K` 这种月薪区间表达的，所以改判数值特征。
    """
    if min_k is None or max_k is None:
        return False
    return max_k < 1.0


def salary_mid(min_k, max_k, months=None) -> float | None:
    """薪资区间均值（K/月），按 12 薪口径折算。缺值返回 None。

    库里 183 条带 ·13薪~·16薪。`15-25K·15薪` 的实际年薪高于 `18-28K·12薪`，
    忽略月数会把前者当低薪。分档、排序、门槛判定必须共用这一个口径。
    """
    if min_k is None or max_k is None:
        return None
    return (min_k + max_k) / 2 * (months or 12) / 12


def salary_raw_mid(min_k, max_k) -> float | None:
    """**月基薪**区间均值（K/月），不折算 salary_months。缺值返回 None。

    专供门槛判定。与 `salary_mid`（12 薪折算，供排序分桶）刻意分开 ——
    见上方 SALARY_DROP_MID_ABOVE 的注释。
    """
    if min_k is None or max_k is None:
        return None
    return (min_k + max_k) / 2


def salary_out_of_range(min_k, max_k, months=None) -> bool:
    """薪资是否超出可达范围。按**月基薪**区间均值判，不按起薪、不折算月数。

    `months` 保留在签名里只为兼容既有调用点，**刻意不使用** ——
    年终奖倍数不改变"月薪能不能谈到"这件事。
    """
    mid = salary_raw_mid(min_k, max_k)
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
