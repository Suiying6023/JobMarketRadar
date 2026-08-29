"""导出投递池为自包含 HTML 网页（可搜索/筛选/排序，浏览器打开即用）。

分页口径（2026-08-10 重构，阈值全部来自 job_radar.profile 单一真源）：
- 硬过滤（任何页都不出现）：已失效/已关闭、非杭州、匿名"某"公司、CV/图像算法、
  未来届(27届+)、猎头发布
- 风险公司（低资本/新成立/非公司主体/工商未知）→ 单列互斥页
- 其余按薪资分四个互斥桶：超纲(起薪>20K) / 低薪(中点≤7K) / 高薪(中点>15K) / 核心
- 核心桶再按活跃天数分 P0(≤3天) / P1(平台未给) / P2(冷)

原先用活跃度文案白名单分 P0/P1，中间档（本周/2周内/本月...）170 条落进裂缝
在任何页面都不出现——改为按 recruiter_active_days 天数分档后不再丢失。

用法: python scripts/export_pool_html.py
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_radar import profile as P  # noqa: E402
from job_radar.normalization import active_days_from_desc  # noqa: E402

DB = "data/hangzhou_agent.sqlite3"

JT_COLOR = {
    "开发岗": "#2563eb", "算法岗": "#7c3aed", "测试岗": "#ea580c",
    "运营岗": "#059669", "产品岗": "#0d9488", "市场岗": "#ca8a04",
    "内容岗": "#dc2626", "设计岗": "#db2777", "其他": "#6b7280",
}
ACT_COLOR = {
    "刚刚活跃": "#dc2626", "今日活跃": "#f97316", "在线": "#16a34a",
    "3日内活跃": "#ea580c",
}

CSS = """
body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:0;background:#f3f4f6;color:#1f2937}
header{background:#111827;color:#fff;padding:18px 24px;position:sticky;top:0;z-index:10}
header h1{margin:0 0 10px;font-size:20px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.controls input,.controls select{padding:7px 10px;border-radius:6px;border:1px solid #374151;background:#1f2937;color:#fff;font-size:13px}
.controls input{flex:1;min-width:200px}
.stats{font-size:12px;color:#9ca3af;margin-top:6px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
th{background:#e5e7eb;padding:9px 10px;text-align:left;cursor:pointer;position:sticky;top:76px;user-select:none}
th:hover{background:#d1d5db}
td{padding:9px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top}
tr:hover{background:#f9fafb}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;color:#fff;margin-right:4px}
.tag{display:inline-block;padding:2px 7px;border-radius:4px;background:#eef2ff;color:#4338ca;font-size:11px;margin:1px 2px}
td:nth-child(7){max-width:280px}
td:nth-child(8){max-width:180px}
.salary{font-weight:600;color:#b91c1c}
.muted{color:#9ca3af}
/* 数据时效：黄=超7天未核实，红=超30天（很可能已下架或活跃度失真） */
.stale-warn{color:#d97706;font-weight:600}
.stale-bad{color:#dc2626;font-weight:600}
details{margin:0}
.jd{font-size:12px;line-height:1.7;max-height:280px;overflow-y:auto;margin-top:6px;padding:8px;background:#f9fafb;border-radius:6px}
.demote{padding:3px 10px;border-radius:6px;border:1px solid #fca5a5;background:#fef2f2;color:#dc2626;font-size:12px;cursor:pointer;white-space:nowrap}
.demote:hover{background:#fee2e2}
tr.marked{opacity:.4}
tr.marked .demote{border-color:#bbf7d0;background:#f0fdf4;color:#16a34a}
"""

JS = """
let sortKey='score', sortDir=-1;  // 默认综合分从高到低
function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const jt=document.getElementById('jt').value;
  let rows=ALL.filter(r =>
    (!jt||r.jt===jt) &&
    (!q||(r.title+r.company+r.tags).toLowerCase().includes(q)));
  // 字符串键必须走 localeCompare：原先一律 (x-y)，字符串相减得 NaN，
  // Array.sort 把 NaN 当 0 处理 → 点「公司」表头完全没反应（静默失效）。
  rows.sort((a,b)=>{
    const x=a[sortKey],y=b[sortKey];
    if(typeof x==='string'||typeof y==='string')
      return String(x||'').localeCompare(String(y||''),'zh')*sortDir;
    return ((x||0)-(y||0))*sortDir;
  });
  const tb=document.getElementById('tbody');
  tb.innerHTML=rows.map((r,i)=>`<tr>
    <td>${i+1}</td>
    <td><span style="font-weight:600;color:${r.score>=6?'#16a34a':r.score>=4.5?'#d97706':'#6b7280'}">${r.score}</span></td>
    <td><a href="${r.url}" target="_blank">${r.title}</a></td>
    <td>${r.riskTag?`<span class="badge" style="background:${r.riskColor}">${r.riskTag}</span><br>`:''}${r.regDetail?`<span title="${r.regDetail}" style="cursor:help">${r.company}</span>`:r.company}<br><span class="muted">${r.size}${r.capital?' · 注册资本'+r.capital:''}${r.founded?' · 成立'+r.founded:''}${r.status?` · <b style="color:${r.statusColor}">${r.status}</b>`:''}</span></td>
    <td class="salary">${r.salary}${r.salary_mid&&/薪/.test(r.salary)?`<br><span class="muted">≈${r.salary_mid}K</span>`:''}</td>
    <td><span class="badge" style="background:${r.actColor}">${r.actLabel}</span>${r.actAge>=7?`<br><span class="muted" title="活跃度是 ${r.actAge} 天前抓到的快照，可能已过期">⚠ ${r.actAge}天前观测</span>`:''}</td>
    <td>${r.district}</td>
    <td><span class="badge" style="background:${r.jtColor}">${r.jt}</span></td>
    <td>${r.tags}</td>
    <td><details><summary class="muted">JD (${r.detail.length>0?Math.round(r.detail.length/2):0}字)</summary><div class="jd">${r.detail}</div></details>
        <span class="muted">首见${r.firstSeen}${r.seen>1?` · 在架${r.seen}次`:''}</span></td>
    <td><span class="${r.actAge>=30?'stale-bad':r.actAge>=7?'stale-warn':''}"
        title="最后一次核实这条数据（采集或刷新）的日期">${r.lastSeen||'—'}</span>
        ${r.actAge!=null?`<br><span class="muted">${r.actAge}天前</span>`:''}</td>
    <td>${r.appliedAt?`<span class="badge" style="background:#16a34a">已投 ${r.appliedAt}</span>`
        :`<button onclick="apply(this,'${r.id}')">投递</button>`}
        <button class="demote" onclick="demote(this,'${r.id}')">排除</button>
        <button onclick="addNote(this,'${r.id}')" title="${r.note||'加备注'}">${r.note?'📝':'✎'}</button></td>
  </tr>`).join('');
  // 时效概览：让「这批数据有多旧」在打开页面第一眼就能看到，
  // 而不是逐行去看「最后核实」列。
  const fresh=rows.filter(r=>r.actAge!=null&&r.actAge<7).length;
  const stale=rows.filter(r=>r.actAge!=null&&r.actAge>=30).length;
  const newest=rows.map(r=>r.lastSeen).filter(Boolean).sort().pop()||'—';
  document.getElementById('stats').innerHTML=
    `显示 ${rows.length} / ${ALL.length} 条 · 最新核实 ${newest}`
    + ` · <span class="stale-warn">7天内 ${fresh}</span>`
    + ` · <span class="stale-bad">超30天 ${stale}</span>`;
}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
async function demote(btn, id){
  try{
    const r = await fetch('/demote', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const j = await r.json();
    if(j.ok){ btn.closest('tr').classList.add('marked'); btn.textContent='撤销'; btn.onclick=()=>restore(btn,id); }
    else alert('降级失败: '+(j.err||'未知'));
  }catch(e){ alert('标记需通过 http:// 服务打开本页'); }
}
async function restore(btn, id){
  try{
    const r = await fetch('/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const j = await r.json();
    if(j.ok){ btn.closest('tr').classList.remove('marked'); btn.textContent='排除'; btn.onclick=()=>demote(btn,id); }
    else alert('撤销失败: '+(j.err||'未知'));
  }catch(e){ alert('标记需通过 http:// 服务打开本页'); }
}
// 「已投递」与「排除」语义不同：投过的置绿保留可见，便于跟进；排除才移出池子。
async function apply(btn, id){
  try{
    const r = await fetch('/apply', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const j = await r.json();
    if(j.ok){
      const d = new Date().toISOString().slice(0,10);
      btn.outerHTML = `<span class="badge" style="background:#16a34a">已投 ${d}</span>`;
    } else alert('标记失败: '+(j.err||'未知'));
  }catch(e){ alert('标记需通过 http:// 服务打开本页'); }
}
async function addNote(btn, id){
  const cur = btn.getAttribute('title')==='加备注' ? '' : btn.getAttribute('title');
  const msg = prompt('备注（HR 回复/面试安排/薪资谈到多少）:', cur);
  if(msg===null) return;
  try{
    const r = await fetch('/note', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id, message:msg})});
    const j = await r.json();
    if(j.ok){ btn.textContent = msg?'📝':'✎'; btn.setAttribute('title', msg||'加备注'); }
    else alert('保存失败: '+(j.err||'未知'));
  }catch(e){ alert('标记需通过 http:// 服务打开本页'); }
}
"""


def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        return [v]
    return []


def extract_capital(intro: str) -> str:
    """从公司简介碰运气提取注册资本（company_reg_json 缺字段时的兜底）。"""
    m = re.search(r"注册资本[^\d]*(\d+(?:\.\d+)?)\s*([万亿])", intro or "")
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return ""


def observed_days_ago(r) -> int | None:
    """活跃度是多少天前观测到的。

    `recruiter_active_status` 是抓详情那一刻的快照，不随时间衰减——一条 30 天前
    记录的「刚刚活跃」今天仍显示「刚刚活跃」并据此判 P0。所以展示与排序都必须
    带上这个年龄（见 P.stale_penalty）。

    基准是 `last_verified()`（采集与详情刷新取较新者）。None = 从未观测。
    """
    newest = last_verified(r)
    if not newest:
        return None
    try:
        return max(0, (date.today() - date.fromisoformat(newest)).days)
    except (ValueError, TypeError):
        return None


def parse_reg(r) -> dict:
    """工商登记信息（enrich_company_info.py 富化，gongsi 页免登录抓取）。"""
    try:
        return json.loads(r["company_reg_json"] or "{}")
    except Exception:
        return {}


def raw_of(r) -> dict:
    """列表接口原始响应。解析失败返回空字典，让调用方回退到字段兜底判断。"""
    try:
        return json.loads(r["raw_json"] or "{}")
    except Exception:
        return {}


def last_verified(r) -> str:
    """最后一次核实这条数据的日期（YYYY-MM-DD），取采集与详情刷新的较新者。

    不能只看 last_seen_at：存量数据里有 144 行因旧版 upsert 把 collected_at
    往回写，使 last_seen 早于 detail_fetched（storage.py 已修，历史数据仍错）。
    """
    stamps = [s for s in ((r["last_seen_at"] or ""), (r["detail_fetched_at"] or ""))
              if s]
    return max(stamps)[:10] if stamps else ""


def status_color(status: str) -> str:
    if status in ("存续", "在业", "开业", "在营", "正常"):
        return "#16a34a"
    if status in ("吊销", "注销", "撤销", "停业", "迁出", "已注销", "清算"):
        return "#dc2626"
    return "#6b7280"


FX_RATE = {"美元": 7.2, "港元": 0.92, "欧元": 7.8, "日元": 0.048, "英镑": 9.1}


def cap_wan(s: str):
    """注册资本字符串 → 万元人民币。如「150万人民币」→150，「5000万美元」→36000。

    外币按近似汇率折算：库里有 43 条外币资本（阿里/快手/涂鸦等），
    原先直接按人民币口径比较——当前都远超 100 万所以没误判，但逻辑不该靠巧合。
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万亿|亿|万)", s or "")
    if not m:
        return None
    v = float(m.group(1))
    v = v * 1e8 if m.group(2) == "万亿" else v * 1e4 if m.group(2) == "亿" else v
    for cur, rate in FX_RATE.items():
        if cur in (s or ""):
            return v * rate
    return v


# 「数据还没抓到」不是「公司有风险」。这两类必须分开，否则每轮新采集的岗位
# 都会因为工商富化滞后而被整批流放到风险页——2026-08-11 实测 133 条新增 active
# 全部只带 `工商未知` 一个标签，其中 48 条「刚刚活跃」，本该是 P0 头部。
UNKNOWN_TAGS = frozenset({"工商未知", "资本未知"})


def risk_tags(reg: dict) -> list:
    """全部标记，含「数据未知」类。分流用 `real_risk_tags()`。"""
    if not reg:
        return ["工商未知"]
    tags = []
    cap_text = reg.get("注册资本", "")
    cap = cap_wan(cap_text)
    if cap is None:
        # 库里有 3 条注册资本是 "-"（平台不给），与真的空值同样视为未知
        tags.append("资本未知")
    elif cap < P.RISK_MIN_CAPITAL_WAN:
        tags.append("低资本")
    est = reg.get("成立时间", "")
    if est and est >= P.risk_cutoff_date():
        tags.append("新成立")
    if any(k in (reg.get("企业类型") or "") for k in P.RISK_ENTITY_MARKS):
        tags.append("非公司主体")
    return tags


def real_risk_tags(reg: dict) -> list:
    """只保留**真实风险**（低资本/新成立/非公司主体），用于风险页分流。

    「工商未知」「资本未知」是采集进度问题，不是公司问题——留在主池里带个
    灰色徽标提示"投前自己看一眼公司页"即可，不该因此看不见这个岗位。
    """
    return [t for t in risk_tags(reg) if t not in UNKNOWN_TAGS]


RISK_TAG_COLOR = {
    "低资本": "#d97706", "新成立": "#0284c7",
    "工商未知": "#6b7280", "资本未知": "#78716c", "非公司主体": "#be123c",
}

def salary_mid(r):
    """薪资中点（K/月），按 salary_months 折算成 12 薪口径。

    库里 183 条带 ·13薪~·16薪。`15-25K·15薪` 的实际年薪高于 `18-28K·12薪`，
    原先忽略月数，分档和排序都把前者当低薪。
    """
    a, b = r["salary_min_k"], r["salary_max_k"]
    if a is None or b is None:
        return None
    months = r["salary_months"] or 12
    return (a + b) / 2 * months / 12


def tech_hits(r) -> int:
    """技术栈命中的**原始个数**（不截断、不乘权重）。

    存在的理由是同分排序：`job_score` 里技术栈项 `×0.5` 后撞 `W_TECH_MAX` 上限，
    实测 17.3% 的岗位顶在上限上（top50 里 40 条），命中 6 个词和命中 12 个词
    在总分上不可区分。把原始个数作为二级排序键，能在不改总分口径的前提下
    把同分组内的顺序从"SQL 返回顺序"换成"对口程度"。

    与 `job_score` 共用同一份匹配逻辑（含 tech_req 为空时回退 JD 正文），
    **不要在别处重写** —— 两份会各自漂移。
    """
    return sum(1 for k in P.AI_TECH if k in _tech_blob(r))


def job_score(r) -> float:
    """综合评分。所有权重取自 profile.py。

    构成：技术栈匹配（最高权重，唯一反映对不对口）+ BOSS 结构化技能交集
    + 活跃度微调 + 公司规模/融资阶段 + 薪资档内位置 + 应届友好 + 城市偏好
    - 销售性质 - 招聘者未认证 - 数据陈旧。

    技术栈用**子串**匹配而非集合交集：LLM 抽的 tech_req 是整句
    （「熟悉python、java、go等至少一种后端语言」），精确交集只命中 126/537，
    子串命中 424/537。标签缺失时回退 JD 正文，否则每轮新采的岗位
    （有 JD 无标签）会系统性拿 0 分排到最后。
    """
    g = json.loads(r["glm_tags_json"] or "{}")

    # 技术栈：LLM 抽的 tech_req（长句，子串匹配）。
    # ⚠️ 标签未提取时**回退到 JD 正文**，不能静默给 0 分：标签提取滞后于采集，
    # 每轮新岗位都是「有 JD 但无标签」状态（2026-08-11 实测 133 条），
    # 静默给 0 会让最新鲜的岗位系统性排在最后——既进不了风险页也上不了榜首。
    tech = min(tech_hits(r) * 0.5, P.W_TECH_MAX)

    # BOSS 结构化技能词（短词，精确交集——比 LLM 抽的更干净）
    try:
        skills = {str(s).lower() for s in (json.loads(r["skills_json"] or "[]") or [])}
    except Exception:
        skills = set()
    skill_hit = min(len(skills & set(P.AI_TECH)) * 0.5, P.W_SKILLS_MAX)

    # 活跃度按快照年龄衰减：19 天前的「在线」不能证明现在在线，也不能证明
    # 不在线 —— 它是无信息（×0），不该按满分参与排序。
    act = (P.ACT_SCORE.get(r["recruiter_active_status"], 0.0)
           * P.freshness(observed_days_ago(r)))
    size = P.SIZE_SCORE.get(r["company_size"], P.SIZE_DEFAULT)
    stage = P.STAGE_SCORE.get(r["financing_stage"], P.STAGE_DEFAULT)

    # 薪资：核心档 7-15K 内线性给分，超出档位截断
    mid = salary_mid(r)
    sal = 0.0
    if mid is not None:
        lo, hi = P.SALARY_LOW_MID_MAX, P.SALARY_HIGH_MID_MIN
        sal = max(0.0, min(1.0, (mid - lo) / (hi - lo))) * P.W_SALARY_MAX

    # 应届友好：要求「1-3年」的岗位对应届生成功率低于「经验不限/在校应届」
    exp_ok = P.W_EXP_FRIENDLY if (r["experience"] or "") in ("经验不限", "在校/应届") else 0.0

    # 城市偏好：白名单决定能否进池，这里决定池内排序（杭州 > 上海 > 深圳）
    city = P.city_score(r["city"] or "") * P.W_CITY_MAX

    penalty = 0.0
    if any(m in (r["welfare_json"] or "") for m in P.SALES_WELFARE_MARKS):
        penalty += P.W_SALES_PENALTY
    if not r["recruiter_certificated"]:
        penalty += P.W_UNCERT_PENALTY
    # 下架风险扣分。与上面 act 的衰减是两件事：那里让过期活跃度退化成无信息，
    # 这里扣的是「久未核实、可能已经招满」的风险。
    penalty += P.stale_penalty(observed_days_ago(r))

    return round(tech + skill_hit + act + size + stage + sal + exp_ok
                 + city + penalty, 1)


def _tech_blob(r) -> str:
    """技术要求文本：优先 LLM 抽的 tech_req，为空时回退 JD 正文。

    与 `tech_hits` / `job_score` 同一口径，别在别处重写。
    """
    g = json.loads(r["glm_tags_json"] or "{}")
    blob = " ".join(str(t) for t in _as_list(g.get("tech_req"))).lower()
    return blob if blob.strip() else (r["job_description"] or "").lower()


def is_cv_job(r) -> bool:
    """岗位主体是否是 CV/图像算法。

    ⚠️ 判据是 **CV 词数 > AI/Agent 词数**，不是"出现 CV 词即命中"。
    原先按 `any` 判，实测 48 条被拦的岗位里 **27 条 AI 词多于 CV 词**，
    最极端的 `AI工程师（视觉与大模型方向）` 是 cv=1 vs **ai=12** ——
    它的 tech_req 里写满 LangChain/CrewAI/Function Calling/LoRA，
    是个 YOLO + LLM/Agent 联合系统岗，被单个"目标检测"一票否决。
    这违反了本项目自己的边界：弱信号只配排序，不配当门槛。

    标题路径保留 `any`：标题是平台方写的岗位定位，`视觉算法工程师` 就是
    CV 岗，实测那 15 条零误杀。但标题命中时仍要看 AI 词是否压倒性更多，
    否则 `AI工程师（视觉与大模型方向）` 这类联合岗还是会被标题拦掉。
    """
    blob = _tech_blob(r)
    n_cv = sum(1 for k in P.CV_TECH_KW if k in blob)
    n_ai = sum(1 for k in P.AI_TECH if k in blob)
    if P.match_keyword(r["title"] or "", P.CV_TITLE_KW):
        # 标题直标 CV，除非内容压倒性偏 AI（差 3 个词以上）
        return n_ai < n_cv + 3
    if any(t.strip().lower() == "cv" for t in
           _as_list(json.loads(r["glm_tags_json"] or "{}").get("tech_req"))):
        return n_ai <= n_cv
    return n_cv > 0 and n_cv >= n_ai


# 设计/美术/内容运营岗的降级判定**不在这里**，在 apply_glm_filters.classify()
# 里按 LLM 的 job_type 判（设计岗/内容岗/运营岗/市场岗）。
#
# 2026-08-29 曾在此加一个 is_nondev_role() 按 JD 关键词计数判，**实测失败并撤回**：
# 17 条被拦的里 16 条是真正的 Agent/大模型开发岗（job_type 全是开发岗、
# LLM 全判 keep），例如 `多智能体应用研发工程师（AI 短剧 Agent）`
# —— 它的 JD 写着"搭建编剧、导演、分镜、素材处理多 Agent 协作系统"。
# 根因：**那些词描述的是 Agent 要自动化的业务对象，不是岗位要人做的事**。
# 做内容生产 Agent 的开发岗，JD 里必然出现剪辑/素材/分镜。
# 而我筛词的样本只有"标题含 AIGC 的 233 条"，这类岗在样本里几乎不存在 ——
# **筛词的样本没有覆盖判据将要作用的全体**，所以那个区分度是假的。
#
# 实测支撑"交给 LLM 就够了"：库里 296 条设计/内容/运营/市场岗
# **全部已在 sample，active 里 0 条**，LLM 判过的 74 条全是 demote、零例外。
# 所以这里不需要再加一层 —— 加了就是一条从未生效却看着像在防的规则，
# 而且与 apply_glm_filters 构成两份会各自漂移的判定。


def is_future_cohort(r) -> bool:
    """校招届数错位剔除：用户 26 届，标题含 27届/2027届 等未来届 → 不投。
    JD 正文通常不提届数（详情接口 postDescription 也不含毕业时间——那是校招岗
    专属的服务端渲染结构化字段），届数只在标题里。2026-08-10 抽查 5 岗验证。"""
    m = re.search(r"(20\d{2}|\d{2})届", r["title"] or "")
    if not m:
        return False
    num = m.group(1)
    yr = int(num) if len(num) == 4 else 2000 + int(num)
    return yr > P.COHORT_YEAR


def hard_drop(r) -> str:
    """硬过滤：任何页面都不该出现的岗位，返回原因（不命中返回 ""）。

    原先这些全部漏进 P0：已失效 5 条、已关闭 4 条、非杭州 7 条、猎头发布。
    """
    if r["job_invalid"]:
        return "已失效"
    if "关闭" in (r["job_status_desc"] or ""):
        return "已关闭"
    if not P.city_allowed(r["city"] or ""):
        return f"非目标城市[{r['city']}]"
    if P.is_anonymous(raw_of(r), r["company_name"] or ""):
        return "匿名公司"
    if P.match_keyword(r["recruiter_title"] or "", P.EXCLUDE_RECRUITER_TITLE):
        return "猎头发布"
    kw = P.match_keyword(r["title"] or "", P.EXCLUDE_TITLE)
    if kw:
        return f"标题[{kw}]"
    # OD 按词边界判（不能进 EXCLUDE_TITLE 的裸子串表，会吃掉 Coding）。
    # 这里必须有：超纲桶是直接从 sample 读的，只过 hard_drop 不过 should_demote，
    # 所以画像层刚降的 OD 岗会从超纲页绕回页面（实测漏回 3 条华为 OD）。
    if P.outsourced_od(r["title"] or ""):
        return "外包[OD]"
    kw = P.match_keyword(r["company_name"] or "", P.EXCLUDE_COMPANY)
    if kw:
        return f"公司[{kw}]"
    if is_cv_job(r):
        return "CV/图像算法"
    if is_future_cohort(r):
        return "未来届"
    if P.is_daily_salary(r["salary_min_k"], r["salary_max_k"]):
        return "日薪(实习)"
    return ""


def build_page(title: str, subtitle: str, rows) -> str:
    data = []
    for r in rows:
        g = json.loads(r["glm_tags_json"] or "{}")
        jt = g.get("job_type") or "?"
        act = r["recruiter_active_status"] or "(空)"
        tech_req, exp_req, edu_req, school_req = (_as_list(g.get(k)) for k in
                                                  ("tech_req", "exp_req", "edu_req", "school_req"))
        tech = "".join(f'<span class="tag">{esc(t)}</span>' for t in tech_req[:4])
        exp = "".join(f'<span class="tag">{esc(e[:14])}</span>' for e in exp_req[:2])
        reg = parse_reg(r)
        capital = reg.get("注册资本") or extract_capital(r["company_intro"])
        status = reg.get("经营状态", "")
        reg_detail = "；".join(f"{k}: {v}" for k, v in reg.items()) if reg else ""
        risk = risk_tags(reg)
        risk_tag = "双风险" if len(risk) == 2 else (risk[0] if risk else "")
        risk_color = "#dc2626" if len(risk) == 2 else RISK_TAG_COLOR.get(risk_tag, "")
        act_label = act
        # 活跃天数：库里有 29 行 status 与 days 不一致（历史脏数据），
        # days 缺失时从文案现场兜底，别让脏数据决定展示。
        act_days = r["recruiter_active_days"]
        if act_days is None:
            act_days = active_days_from_desc(act_label)
        act_age = observed_days_ago(r)
        data.append({
            "title": esc(r["title"]), "company": esc(r["company_name"]),
            "salary": esc(r["salary_text"]), "district": esc(r["district"]),
            "jt": esc(jt), "tags": tech + exp,
            "exp": esc("、".join(edu_req[:2] + school_req[:1])),
            "url": esc(r["job_url"]), "salary_max": r["salary_max_k"] or 0,
            "salary_min": r["salary_min_k"] or 0,
            # 薪资排序必须用折算成 12 薪口径的中点，和分桶同一口径。
            # 原先按 salary_max_k 排，忽略 ·N薪 → 实测 `15-25K·15薪`（中点 25.0）
            # 会排在 `18-30K`（中点 24.0）下面，与页面自己的分桶规则矛盾。
            "salary_mid": round(salary_mid(r) or 0, 1),
            "id": esc(r["canonical_id"]),
            "size": esc(r["company_size"] or ""),
            "capital": esc(capital),
            "founded": esc(reg.get("成立时间", "")),
            "status": esc(status),
            "statusColor": status_color(status),
            "regDetail": esc(reg_detail),
            "riskTag": risk_tag,
            "riskColor": risk_color,
            "score": job_score(r),
            "detail": esc(r["job_description"]).replace("\n", "<br>"),
            "jtColor": JT_COLOR.get(jt, "#6b7280"),
            # 活跃度上页面（原先只体现在"你打开了哪个文件"这一层，页面里
            # 一个字都没有）。招聘者活跃度对投递回复率影响极大，而高薪页
            # 199 条把「刚刚活跃」和「半年前活跃」平铺在一起，恰恰是最大的
            # 那一页丢掉了最强的信号。ACT_COLOR 早就定义好了却从未被用。
            "actLabel": esc(act_label),
            "actDays": act_days,
            "actColor": ACT_COLOR.get(act_label, "#6b7280"),
            # 活跃度是抓详情那一刻的快照，会过期。展示"观测于几天前"让用户
            # 自己判断这个"刚刚活跃"还值不值得信。
            "actAge": act_age,
            "seen": r["seen_count"] or 0,
            "firstSeen": (r["first_seen_at"] or "")[:10],
            # 最后一次真正核实这条数据的时刻（采集或刷新，取较新者）。
            # 这是判断「岗位是否还在挂着」的唯一依据，必须上页面并可排序。
            "lastSeen": last_verified(r),
            "appliedAt": (r["applied_at"] or "")[:10],
            "note": esc(r["note"] or ""),
        })
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<header><h1>{esc(title)}</h1>
<div class="controls">
<input id="q" placeholder="搜索岗位 / 公司 / 技术栈..." oninput="render()">
<select id="jt" onchange="render()"><option value="">全部类型</option>
{''.join(f'<option>{j}</option>' for j in JT_COLOR)}</select>
</div>
<div class="stats" id="stats"></div></header>
<table><thead><tr>
<th>#</th><th onclick="sortBy('score')">分</th><th>岗位</th><th onclick="sortBy('company')">公司</th>
<th onclick="sortBy('salary_mid')">薪资</th><th onclick="sortBy('actDays')">活跃度</th><th>区域</th><th>类型</th><th>技术/经验</th><th>岗位详情</th><th onclick="sortBy('actAge')">最后核实</th><th>操作</th>
</tr></thead><tbody id="tbody"></tbody></table>
<script>const ALL={json.dumps(data, ensure_ascii=False)};{JS}render();</script>
<p class="muted" style="padding:12px">{esc(subtitle)}</p>
</body></html>"""


def dedupe(rows: list) -> tuple[list, int]:
    """同一岗位被 BOSS 以不同 encryptJobId 重复发布 → 按 (标题, brand_id) 二次去重。

    canonical_id 基于 source_job_id，抓不到这种重复。实测 9 组重复冗余 14 条
    （多益「AI应用开发工程师【27届校招】」4 份、赢云贸易 SDE04 4 份）。
    保留活跃天数最小（最新）的那条。
    """
    best: dict[tuple, object] = {}
    for r in rows:
        key = ((r["title"] or "").strip().lower(), r["brand_id"] or r["company_name"])
        cur = best.get(key)
        if cur is None or (r["recruiter_active_days"] if r["recruiter_active_days"] is not None else 999) < \
                (cur["recruiter_active_days"] if cur["recruiter_active_days"] is not None else 999):
            best[key] = r
    return list(best.values()), len(rows) - len(best)


def salary_bucket(r) -> str:
    """薪资四桶，互斥且穷尽。

    「超纲」必须先判：否则高于上限的岗位会先被归进高薪页，
    与页面副标题声明的口径自相矛盾。
    """
    if P.salary_out_of_range(r["salary_min_k"], r["salary_max_k"], r["salary_months"]):
        return "over"
    mid = salary_mid(r)
    if (mid or 99) <= P.SALARY_LOW_MID_MAX:
        return "low"
    if (mid or 0) > P.SALARY_HIGH_MID_MIN:
        return "high"
    return "core"


def active_tier(r) -> str:
    """活跃度分档，按天数而非文案白名单。

    原先用文案白名单，中间档（本周/2周内/本月/2月内/3月内/4月内/5月内/近半年/
    半年前）170 条既不属 P0 也不属 P1，在任何页面都不出现。
    """
    d = r["recruiter_active_days"]
    if d is None:
        return "p1"
    return "p0" if d <= P.ACTIVE_P0_MAX_DAYS else "p2"


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # 投递池 = active OR bookmarked（原先只查 active，与 models.py 注释的口径不符：
    # 画像外岗位一旦被手工标记浏览过，应保留为投递目标）
    # LEFT JOIN 出投递状态：投过的岗位要置灰但保留可见（跟进沟通状态用），
    # 这是连续多天求职时最需要的一列——否则第二天打开页面不知道昨天投了哪些。
    #
    # ⚠️ 口径说明（2026-08-11 修）：投递池主体是 `active OR bookmarked`，但
    # **超纲页必须额外读 sample 里起薪>20K 的岗位**。`clean_local_db` 按画像把
    # 起薪>20K 降为 sample（对双非应届，阿里云 30-60K、闲鱼 45-70K 确实够不着，
    # 这个降级判断是对的），可 export 又单开一页声称展示超纲岗位却只读 active
    # —— 490 条超纲岗全在 sample 里，那页永远只能显示 69 条 active 里剩下的零头。
    # 两层过滤在打架。修法不是把它们放回 active（会污染主池排序），
    # 而是让超纲页读它该读的数据源：留档参考用，不进主池。
    JOIN_ACTIONS = (
        "LEFT JOIN ("
        "  SELECT canonical_id,"
        "         MAX(CASE WHEN action='applied' THEN created_at END) applied_at,"
        "         MAX(CASE WHEN action='note' THEN message END) note"
        "  FROM chat_actions GROUP BY canonical_id"
        ") a ON a.canonical_id = j.canonical_id "
    )
    rows = con.execute(
        f"SELECT j.*, a.applied_at, a.note FROM jobs j {JOIN_ACTIONS}"
        "WHERE (j.retention_status='active' OR j.bookmarked=1) "
        "AND j.job_description != ''"
    ).fetchall()
    total_raw = len(rows)
    # 超纲岗单独取（含 sample）：它们不进主池分桶，只在超纲页留档
    over_rows = con.execute(
        f"SELECT j.*, a.applied_at, a.note FROM jobs j {JOIN_ACTIONS}"
        "WHERE j.retention_status='sample' AND j.bookmarked=0 "
        "AND j.job_description != '' AND j.job_invalid = 0 "
        # 与 P.salary_out_of_range 同口径：**月基薪**均值，不折算 salary_months
        "AND j.salary_min_k IS NOT NULL AND j.salary_max_k IS NOT NULL "
        "AND (j.salary_min_k + j.salary_max_k) / 2.0 > ?",
        (P.SALARY_DROP_MID_ABOVE,),
    ).fetchall()

    # 1) 硬过滤
    drops: dict[str, int] = {}
    kept = []
    for r in rows:
        why = hard_drop(r)
        if why:
            drops[why] = drops.get(why, 0) + 1
        else:
            kept.append(r)

    # 2) 重复岗位二次去重
    kept, dup_n = dedupe(kept)

    # 3) 风险公司互斥单列。**只按真实风险分流**（见 real_risk_tags 注释）：
    #    工商数据没抓到的岗位留在主池，带灰色「工商未知」徽标即可。
    risk_jobs = [r for r in kept if real_risk_tags(parse_reg(r))]
    clean = [r for r in kept if not real_risk_tags(parse_reg(r))]
    risk_jobs.sort(key=lambda r: (-len(real_risk_tags(parse_reg(r))),
                                  cap_wan(parse_reg(r).get("注册资本", "")) or 9e9))

    # 4) 薪资四桶 → 核心桶再按活跃度分 P0/P1/P2
    buckets: dict[str, list] = {"core": [], "low": [], "high": [], "over": []}
    # sample 里的超纲岗并入超纲页（它们没经过主池的硬过滤，这里补一道，
    # 否则匿名公司/CV算法/未来届会漏进来）
    over_extra = 0
    for r in over_rows:
        if not hard_drop(r):
            buckets["over"].append(r)
            over_extra += 1
    for r in clean:
        buckets[salary_bucket(r)].append(r)
    tiers: dict[str, list] = {"p0": [], "p1": [], "p2": []}
    for r in buckets["core"]:
        tiers[active_tier(r)].append(r)
    tiers["p2"].sort(key=lambda r: r["recruiter_active_days"] or 999)

    out = Path("output")
    out.mkdir(exist_ok=True)
    # 导出日期取当天。原先写死 "2026-08-10"，于是之后每次重导出，页面副标题
    # 都仍声称数据是 08-10 的——而活跃度是抓详情那刻的快照，页面上这个日期
    # 正是读者判断「这个活跃度还新鲜吗」的唯一依据，写死等于持续误导。
    today = date.today().isoformat()
    pages = [
        ("投递池_P0优先.html", "杭州 AI/Agent 投递池 · P0 优先投递",
         f"核心薪资档(7-15K) 且 3日内活跃；按综合分排序。硬过滤 {sum(drops.values())} 条、"
         f"风险公司 {len(risk_jobs)} 条、高/低/超纲薪资已单列；数据 {today}", tiers["p0"]),
        ("投递池_P1待确认.html", "杭州 AI/Agent 投递池 · P1 待确认",
         f"核心薪资档，但平台未返回活跃度（需补详情确认）；数据 {today}", tiers["p1"]),
        ("投递池_P2冷池.html", "杭州 AI/Agent 投递池 · P2 冷池（活跃度较低）",
         f"核心薪资档，招聘者最后活跃超过 {P.ACTIVE_P0_MAX_DAYS} 天，按活跃天数升序。"
         f"原先这批在任何页面都不出现；数据 {today}", tiers["p2"]),
        ("投递池_高薪.html",
         f"投递池 · 高薪岗位（中点>{P.SALARY_HIGH_MID_MIN:.0f}K，12薪折算）",
         f"均值未超 {P.SALARY_DROP_MID_ABOVE:.0f}K，含 ·13薪+ 折算；数据 {today}",
         buckets["high"]),
        ("投递池_低薪.html", f"投递池 · 低薪岗位（中点≤{P.SALARY_LOW_MID_MAX:.0f}K）",
         f"薪资中点偏低，已从核心池移出；数据 {today}", buckets["low"]),
        ("投递池_超纲.html",
         f"投递池 · 薪资均值>{P.SALARY_DROP_MID_ABOVE:.0f}K（超出可达范围）",
         f"按区间均值折算 12 薪判定，仅留档参考；数据 {today}", buckets["over"]),
        ("投递池_风险公司.html", "投递池 · 风险公司岗位（互斥）",
         f"低资本<{P.RISK_MIN_CAPITAL_WAN:.0f}万 / 成立<{P.RISK_MIN_AGE_MONTHS}月 / "
         f"非公司主体 / 工商未知；多重风险置顶；数据 {today}", risk_jobs),
    ]
    for fname, title, sub, data in pages:
        (out / fname).write_text(build_page(title, sub, data), encoding="utf-8")

    print(f"投递池原始 {total_raw} 条 → 硬过滤 {sum(drops.values())} + 重复 {dup_n} → 剩 {len(clean) + len(risk_jobs)}")
    print("硬过滤原因:", ", ".join(f"{k}={v}" for k, v in sorted(drops.items(), key=lambda x: -x[1])))
    print(f"已导出: P0({len(tiers['p0'])}) P1({len(tiers['p1'])}) P2({len(tiers['p2'])}) | "
          f"高薪({len(buckets['high'])}) 低薪({len(buckets['low'])}) 超纲({len(buckets['over'])}) | "
          f"风险({len(risk_jobs)})")
    # 互斥性校验：主池每条必须且只能落进一个桶。
    # 减去 sample 来源的超纲岗——它们不来自 clean，不参与主池互斥。
    over_from_main = len(buckets["over"]) - over_extra
    assert len(clean) == sum(len(v) for v in tiers.values()) + len(buckets["high"]) + \
        len(buckets["low"]) + over_from_main, "分桶不互斥或有遗漏"
    con.close()


if __name__ == "__main__":
    main()
