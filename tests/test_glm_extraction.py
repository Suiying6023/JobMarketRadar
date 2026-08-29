"""抽取结果校验的判据（`scripts/extract_glm_tags.py` 的 validate_tags）。

为什么值得钉测试：`json_object` 只保证是合法 JSON，**不保证字段齐全或枚举合法**，
"能 json.loads"不等于"能用"。而最容易被当成无害而放宽的就是
「match_reason 为空」—— 它恰恰是漏判的特征信号，见下面 test_empty_reason_* 的注释。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "extract_glm_tags", ROOT / "scripts" / "extract_glm_tags.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
validate_tags = _mod.validate_tags
SYSTEM_PROMPT = _mod.SYSTEM_PROMPT


def good_tags(**over) -> dict:
    """一条合法的抽取结果，可用 kwargs 覆盖单个字段。"""
    base = {
        "exp_req": ["经验不限"],
        "edu_req": ["本科及以上"],
        "school_req": [],
        "tech_req": ["Python", "RAG"],
        "other_req": [],
        "bonus": [],
        "job_type": "开发岗",
        "grad_year": "不限",
        "work_schedule": "双休",
        "fresh_grad": "接受应届",
        "match_evidence": "无排除项相关表述",
        "match_reason": "经验不限、本科、双休、AI 开发岗，12 条排除项均不命中",
        "match": "keep",
    }
    base.update(over)
    return base


def test_good_tags_pass():
    assert validate_tags(good_tags()) is None


@pytest.mark.parametrize("field", ["exp_req", "edu_req", "tech_req", "job_type",
                                   "grad_year", "work_schedule", "match"])
def test_missing_required_field_rejected(field):
    tags = good_tags()
    del tags[field]
    err = validate_tags(tags)
    assert err is not None and "MISSING" in err


@pytest.mark.parametrize("field,bad", [
    ("match", "maybe"),
    ("match", "KEEP"),          # 大小写也算非法，下游按字面比较
    ("grad_year", "26届"),      # 少了"可投"，不在枚举里
    ("work_schedule", "周末双休"),
    ("work_schedule", "月休5天"),   # 真实案例：模型如实照抄 JD 用词，须归到"其他非双休"
    ("fresh_grad", "看情况"),
])
def test_bad_enum_rejected(field, bad):
    err = validate_tags(good_tags(**{field: bad}))
    assert err is not None and "BAD_ENUM" in err


@pytest.mark.parametrize("val", ["双休", "大小周", "单休", "其他非双休", "未提及"])
def test_work_schedule_enum_accepts_all_five(val):
    """「其他非双休」是 2026-08-12 补的第五个值。

    起因：一条 JD 写「12：00—21：00 月休5天」，模型如实返回"月休5天"被 BAD_ENUM 卡住 ——
    **模型判断是对的，是枚举太窄**。这类写法同样命中画像第 6 条「非双休」排除项，
    若强行归到"未提及"会丢掉排除依据。
    """
    assert validate_tags(good_tags(work_schedule=val)) is None


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_reason_rejected_even_when_demote(empty):
    """★ 判 demote 但没给理由 —— 必须拒。

    2026-08-11 对照实验的核心教训：候选模型 laguna-s-2.1-free 在 60 条里把 13 条
    该 demote 的岗位放成 keep，那 13 条的 match_reason **全是空字符串**。
    空理由 = 模型没找依据就下结论，不是"懒得写"。
    """
    err = validate_tags(good_tags(match="demote", match_reason=empty))
    assert err is not None and "EMPTY match_reason" in err


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_reason_rejected_when_keep(empty):
    """keep 也要给理由。

    旧 prompt 曾写"否则 keep，match_reason 留空"，于是无法区分
    「通读全文确认干净」和「压根没看」。对 26 届应届生来说，
    放过一条要求工作经验的岗位，代价远高于误杀一条。
    """
    err = validate_tags(good_tags(match="keep", match_reason=empty))
    assert err is not None and "EMPTY match_reason" in err


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_evidence_rejected(empty):
    err = validate_tags(good_tags(match_evidence=empty))
    assert err is not None and "EMPTY match_evidence" in err


def test_prompt_orders_evidence_before_verdict():
    """prompt 里的输出格式必须让证据/理由排在 match 之前。

    字段顺序就是自回归生成顺序：match 在前会让模型先承诺结论再补理由，
    JSON 合法但结论可能是事后合理化，校验层抓不到。
    """
    fmt_line = next(ln for ln in SYSTEM_PROMPT.splitlines()
                    if '"match_evidence"' in ln and '"match"' in ln)
    assert fmt_line.index('"match_evidence"') < fmt_line.index('"match_reason"')
    assert fmt_line.index('"match_reason"') < fmt_line.index('"match":')


def test_prompt_still_lists_all_exclusions():
    """12 条排除项不能在改 prompt 时被顺手删掉（画像的核心就是这些）。"""
    for n in range(1, 13):
        assert f"{n}." in SYSTEM_PROMPT or f"  {n}." in SYSTEM_PROMPT
    for kw in ("不招应届", "985", "硕士", "双休", "外包", "电销"):
        assert kw in SYSTEM_PROMPT


# ---------- 值内裸引号：唯一实测到的 JSON 解析失败原因 ----------

def test_lenient_json_repairs_unescaped_inner_quotes():
    """模型逐字摘录 JD 时会吐出值内裸引号，截断字符串导致整条解析失败。

    实测三次抽样全是这个原因，而且**判定内容本身是对的** —— 直接丢掉重试
    既浪费一次调用，也未必好转（JD 里的引号还在）。所以修好再用。
    """
    broken = ('{"match_evidence":"1年以上音视频测试经验。",'
              '"match_reason":"JD硬性要求"1年以上音视频测试经验"，命中第1条。",'
              '"match":"demote"}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    parsed = _mod._loads_lenient(broken)
    assert parsed["match"] == "demote"
    assert "1年以上" in parsed["match_reason"]


@pytest.mark.parametrize("payload,expect", [
    ('{"r":"正常文本","match":"keep"}', "keep"),
    ('{"r":"写了"2 年及以上"和"本科"","match":"demote"}', "demote"),
    (r'{"r":"已正确转义的\"引号\"","match":"keep"}', "keep"),
    ('{"r":"中文引号「2年以上」不受影响","match":"keep"}', "keep"),
])
def test_lenient_json_preserves_valid_payloads(payload, expect):
    """修复逻辑不能破坏本来就合法的 JSON（含已正确转义的引号）。"""
    assert _mod._loads_lenient(payload)["match"] == expect


def test_prompt_asks_for_chinese_quotes():
    """双管齐下：解析层能修，也要让模型少产生这种输出。"""
    assert "「」" in SYSTEM_PROMPT
    assert "不要用英文双引号" in SYSTEM_PROMPT


def test_prompt_treats_year_requirement_as_default_hit():
    """经验年限必须是「默认命中、有出口才放过」。

    反过来写（"无应届出口时才命中"）会被模型读成"没写不招应届就是有出口"，
    而绝大多数 JD 都不会专门写那句 —— 实测这样改完，20 条本已判对的
    掉了 2 条。这个方向性表述必须钉住。
    """
    assert "默认命中" in SYSTEM_PROMPT
    assert "不是**出口" in SYSTEM_PROMPT or "不是出口" in SYSTEM_PROMPT


def test_prompt_does_not_treat_regular_bachelor_as_gate():
    """求职者是统招全日制本科（双非），这类要求他满足，不能当排除项。"""
    assert "统招本科" in SYSTEM_PROMPT and "全日制本科" in SYSTEM_PROMPT
    idx = SYSTEM_PROMPT.find("统招本科")
    around = SYSTEM_PROMPT[max(0, idx - 200):idx + 200]
    assert "不是" in around or "满足" in around
