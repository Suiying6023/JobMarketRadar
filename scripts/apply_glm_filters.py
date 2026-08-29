"""LLM 标签二次筛选：基于 glm_tags_json 把不符合用户画像的 active 岗位降级为 sample。

降级判断主口径是 **LLM 的 match 字段**——extract_glm_tags.py 在提取标签时已按
用户画像输出 match（demote 即降级），本脚本直接采用。仅当岗位没有 match 字段
（尚未被 LLM 判断）时，才回退到关键词规则兜底（名校/硕士/不招应届/大小周/电销
/非软件岗/运营市场设计内容岗/AIGC/仅27届/仅实习等）。

事实性规则（读 DB 字段，不依赖 LLM）：薪资过低(<7K)、华为。

依赖：glm_tags_json 已由 scripts/extract_glm_tags.py 填充。
列名里的 glm 是历史遗留，与当前用的模型无关（见 .env 的 LLM_MODEL）。

用法:
  python scripts/apply_glm_filters.py --dry-run   # 预览将降级数量
  python scripts/apply_glm_filters.py             # 实际执行降级
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SCHOOL_KW = ("985", "211", "一本", "双一流", "重点高校", "重点院校", "知名院校", "世界名校")
# 应届豁免信号：JD 中出现这些词视为「接受应届/可放宽」，不算硬性冲突
WAIVE = ("不限", "均可", "亦可", "可放宽", "放宽", "欢迎", "优先", "毕业时间",
         "接受应届", "也可考虑", "可酌情", "也可", "可投", "实习经历突出",
         "优秀应届", "优秀者可", "可考虑", "可沟通", "应届优秀者", "应届毕业生")


def is_hard_exp_conflict(rule_exp: list, glm_exp: list) -> bool:
    """规则标应届 且 JD 硬性要求经验（不接受应届 / ≥3年且无豁免）。"""
    if "接受应届/无经验" not in rule_exp:
        return False
    if any("不接受" in e and "应届" in e for e in glm_exp):
        return True
    years = []
    for e in glm_exp:
        m = re.search(r"(\d+)[-~至到]?(\d+)?\s*年", e)
        if m:
            years.append((int(m.group(1)), int(m.group(2) or 0)))
    if any(any(k in e for k in WAIVE) for e in glm_exp):
        return False
    max_y = max((b for _a, b in years), default=0)
    min_y = min((a for a, _b in years), default=0)
    return max_y >= 3 or min_y >= 2


# 非软件类角色词：商品/辅料/采购/商务/市场/货代/电销等业务岗，用户只要 AI/编程开发（2026-08-05 确认）
NON_SW_ROLE_KW = ("商品开发", "辅料", "面料", "饰品", "珠宝", "采购开发", "采购", "商务拓展",
                  "商务专员", "海外商务", "跟单", "渠道开发", "招商", "外贸", "企划",
                  "供应商开发", "资源开发", "市场开发", "货代", "出纳", "会计", "电话销售",
                  "电销", "面销")
# 电销/电话销售出现在标题或 JD 正文即降级（用户只要不碰电话销售岗）
SALES_PHONE_KW = ("电销", "电话销售")
# 不招应届信号：JD/LLM 明确排除应届
# 注意"社招"裸词不算（"社招全职/应届生/实习生"三选一是接受的）
FRESH_GRAD_EXCLUDE = (
    r"不招应届", r"不接受应届", r"不招聘应届", r"非应届",
    r"暂未开放.{0,6}(在校生|应届)", r"仅实习经历请勿投递",
    r"实习经历?不计", r"必须有实际工作经验",
)
# 应届豁免信号：JD 写"优秀应届可放宽"时不算不招应届
FRESH_GRAD_WAIVE = ("优秀应届可放宽", "优秀应届生可放宽", "应届优秀者可",
                    "可放宽至应届", "优秀者应届可", "接受应届优秀")
# 非双休工作制：JD 提到大小周/单休/996 等即降级（用户要双休，2026-08-05 确认）
NON_DOUBLE_REST = (r"大小周", r"单休", r"做六休一", r"上六休一",
                   r"六天工作", r"6天工作", r"九九六", r"996", r"轮休")
# 外包/派遣：JD 内容明确本岗位为外包（区别于"对接外包团队"等上下文）
OUTSOURCING = ("外包岗位", "本岗位为外包", "岗位外包", "外包性质", "劳务派遣", "人力外包",
               "派遣员工", "外派到", "派驻到", "外包到", "项目外包", "驻场开发")


# LLM 的 match_reason 是自由文本，每条都不一样。原因分布必须按**门槛类型**
# 归类，否则计数器退化成"每条一个桶"——实测 319 条降级打出 200 多行几乎不
# 重复的输出，人无法从中看出「哪类门槛卡掉最多岗位」。顺序即优先级：
# 先判具体门槛，「方向不符」放最后兜底（它的措辞最泛，会误吃前面几类）。
LLM_REASON_PATTERNS = (
    ("经验年限", re.compile(r"\d\s*年(以上|及以上|以上工作|经验)|至少\s*\d\s*年"
                            r"|\d\s*年.{0,4}经验|工龄|正式工作经验")),
    ("届别限制", re.compile(r"不招应届|非应届|不考虑应届|暂不.{0,4}应届"
                            r"|20\d\d\s*届|应届出口")),
    ("名校门槛", re.compile(r"985|211|双一流|名校|知名院校|重点高校|海外知名|QS")),
    ("硕士以上", re.compile(r"硕士|研究生|博士")),
    ("非双休", re.compile(r"大小周|单休|做六休一|月休|996|轮休")),
    ("外包驻场", re.compile(r"外包|外派|驻场|派遣|驻地")),
    ("英语/职称", re.compile(r"英语|四级|六级|职称|中级或副高")),
    ("JD信息不足", re.compile(r"信息不足|信息极度不足|不完整|无法判断")),
    ("方向不符", re.compile(r"不符合|不属于|非.{0,6}(开发|编程)岗|范畴|匹配度"
                            r"|销售|运营|教学|讲师|培训|运维|标注")),
)


def llm_reason_bucket(reason: str) -> str:
    """把一段 match_reason 归到有限的门槛类别，用于原因分布统计。"""
    for label, pat in LLM_REASON_PATTERNS:
        if pat.search(reason):
            return f"LLM:{label}"
    return "LLM:其他"


def classify(r) -> tuple[str, str]:
    """返回 (是否降级 yes/no, 原因)。

    主判断用 LLM 在提取阶段按用户画像输出的 match，不依赖关键词规则。
    仅当岗位没有 match 字段（尚未被 LLM 判断）时，才回退到关键词兜底。
    """
    # 事实性规则（DB 字段，非 JD 文本）：薪资过低、华为
    maxk = r["salary_max_k"]
    if maxk and maxk < 7:
        return "yes", "薪资过低(<7K)"
    if "华为" in (r["company_name"] or ""):
        return "yes", "华为"

    g = json.loads(r["glm_tags_json"] or "{}")
    match = str(g.get("match") or "").strip().lower()
    if match:
        if match == "demote":
            return "yes", llm_reason_bucket(str(g.get("match_reason") or ""))
        return "no", ""

    # ---- 未 LLM 判断的岗位：关键词规则兜底 ----
    other_req = " ".join(g.get("other_req") or [])
    # 非软件类业务岗（标题级判断）
    for kw in NON_SW_ROLE_KW:
        if kw in (r["title"] or ""):
            return "yes", "非软件类岗位"
    # 电销/电话销售：标题或 JD 正文
    blob = (r["title"] or "") + " " + (r["job_description"] or "")
    if any(k in blob for k in SALES_PHONE_KW):
        return "yes", "电销/电话销售"
    # 硬性英语四级以上
    if any(k in other_req.lower() for k in ("英语四级", "英语六级", "四六级", "cet-4", "cet4", "cet-6", "cet6")):
        return "yes", "硬性英语四级以上"
    # 不招应届（带豁免保护）
    blob2 = (r["job_description"] or "") + " " + (r["glm_tags_json"] or "")
    if not any(w in blob2 for w in FRESH_GRAD_WAIVE) and any(re.search(p, blob2) for p in FRESH_GRAD_EXCLUDE):
        return "yes", "不招应届"
    # 非双休工作制
    if any(re.search(p, blob2) for p in NON_DOUBLE_REST):
        return "yes", "非双休(大小周/单休等)"
    # 外包/派遣：JD 内容明确本岗位为外包（用户 2026-08-05 确认）
    blob3 = (r["title"] or "") + " " + (r["job_description"] or "")
    if any(k in blob3 for k in OUTSOURCING):
        return "yes", "外包/派遣"
    rule = json.loads(r["jd_tags_json"] or "{}")
    if any(any(k in s for k in SCHOOL_KW) for s in (g.get("school_req") or [])):
        return "yes", "名校要求(双非)"
    # 硕士及以上学历（关键词覆盖 硕士/博士/研究生 变体）
    if any(("硕士" in e or "博士" in e or "研究生" in e) for e in (g.get("edu_req") or [])):
        return "yes", "硕士及以上学历"
    gy = str(g.get("grad_year") or "")
    if "仅27届" in gy:
        return "yes", "仅限27届"
    if "仅实习" in gy:
        return "yes", "仅限实习"
    g_exp = g.get("exp_req") or []
    if any(("不招聘应届" in e or "不接受应届" in e or "不招应届" in e or "非应届" in e) for e in g_exp):
        return "yes", "不招应届"
    jt = g.get("job_type", "")
    if jt == "设计岗":
        return "yes", "设计岗"
    if jt == "内容岗":
        return "yes", "内容岗"
    if jt == "运营岗":
        return "yes", "运营岗"
    if jt == "市场岗":
        return "yes", "市场岗"
    if jt == "产品岗":
        # 只保留 AI/Agent 产品岗；文创/电商/服饰/五金等跑题产品岗降级
        ai_signals = ("ai", "agent", "智能", "大模型", "prompt", "llm", "数字人",
                      "多模态", "深度学习", "机器学习")
        _title = (r["title"] or "").lower()
        _tech = " ".join(g.get("tech_req") or []).lower()
        if not any(s in _title or s in _tech for s in ai_signals):
            return "yes", "非AI产品岗"
    # AIGC 岗位（标题含 AIGC）
    if "aigc" in (r["title"] or "").lower():
        return "yes", "AIGC岗位"
    if is_hard_exp_conflict(rule.get("exp", []), g.get("exp_req") or []):
        return "yes", "硬性经验冲突"
    return "no", ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只预览不执行")
    parser.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT canonical_id, title, company_name, salary_max_k, glm_tags_json, jd_tags_json, "
        "job_description "
        # bookmarked=1 豁免：用户手工判断优先于 LLM 判断，否则手工撤销的岗位
        # 会在本脚本下次重跑时被静默再次降级
        "FROM jobs WHERE retention_status = 'active' AND bookmarked = 0"
    ).fetchall()

    to_demote: list[tuple[str, str]] = []
    reason_counter: dict[str, int] = {}
    for r in rows:
        flag, reason = classify(r)
        if flag == "yes":
            to_demote.append((r["canonical_id"], r["title"]))
            reason_counter[reason] = reason_counter.get(reason, 0) + 1

    print(f"active 岗位: {len(rows)}")
    print(f"将降级: {len(to_demote)} 条（保留 {len(rows) - len(to_demote)}）")
    print("\n原因分布:")
    for why, n in sorted(reason_counter.items(), key=lambda kv: -kv[1]):
        print(f"  {why:22} {n:4}  {n / max(len(to_demote), 1):5.1%}")
    print("\n样例:")
    for cid, title in to_demote[:10]:
        print(f"  {title[:36]}")

    if args.dry_run:
        print("\n[DRY-RUN] 未执行")
    else:
        cur = con.executemany(
            "UPDATE jobs SET retention_status='sample' WHERE canonical_id = ?",
            [(cid,) for cid, _t in to_demote],
        )
        con.commit()
        print(f"\n已降级 {len(to_demote)} 条")
    con.close()


if __name__ == "__main__":
    main()
