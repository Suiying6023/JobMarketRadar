import re
from pathlib import Path

import pytest

from job_radar.normalization import (
    ACTIVE_DESC_DAYS, CLOSED_MARKERS, active_days_from_desc,
    parse_salary, source_id_from_url,
)

ROOT = Path(__file__).resolve().parent.parent.parent


def test_parse_salary_k_months():
    assert parse_salary("20-35K·14薪") == (20.0, 35.0, 14)


def test_parse_salary_wan():
    assert parse_salary("2-3万") == (20.0, 30.0, None)


def test_source_id_from_url():
    assert source_id_from_url("https://www.zhipin.com/job_detail/abc123.html") == "abc123"


# ---- 活跃度文案表 ----

# 库里实际出现过的全部 13 种文案（现场统计），逐个钉住。
@pytest.mark.parametrize("phrase,days", [
    ("在线", 0), ("刚刚活跃", 0), ("今日活跃", 0),
    ("3日内活跃", 3), ("本周活跃", 7), ("2周内活跃", 14),
    ("本月活跃", 30), ("2月内活跃", 60), ("3月内活跃", 90),
    ("4月内活跃", 120), ("5月内活跃", 150),
    ("近半年活跃", 180), ("半年前活跃", 180),
])
def test_active_desc_maps_to_expected_days(phrase, days):
    assert active_days_from_desc(phrase) == days


def test_longer_phrases_win_over_shorter_ones():
    """匹配必须按短语长度降序。

    短语之间互相包含（`本月活跃` 与 `2月内活跃` 都含"月活跃"），
    短的排前面会吃掉长的，静默改变一批已有岗位的降级判定。
    """
    lengths = [len(p) for p, _ in ACTIVE_DESC_DAYS]
    assert lengths == sorted(lengths, reverse=True)


def test_no_bare_phrase_shadows_the_numeric_fallback():
    """不许加"裸文案"条目（`月内活跃`、`日内活跃`、`半年内活跃`）。

    它们是带数字组合的子串，会先命中并挡掉正则兜底：加了 `月内活跃` 之后
    `6月内活跃` 返回 30 而不是 180。库里 13 种实际文案也从未出现裸写法。
    """
    bare = [p for p, _ in ACTIVE_DESC_DAYS
            if p in ("日内活跃", "月内活跃", "周内活跃", "年内活跃", "半年内活跃")]
    assert not bare, f"这些裸文案会挡掉正则兜底: {bare}"
    assert active_days_from_desc("6月内活跃") == 180
    assert active_days_from_desc("7日内活跃") == 7


def test_unknown_and_empty_stay_none():
    """判不了就返回 None，别猜。

    `None` 与 `0` 语义不同：0 是「今天还活跃」，None 是「平台没给」。
    混淆会让缺失值排到最活跃的位置。
    """
    assert active_days_from_desc("") is None
    assert active_days_from_desc(None) is None
    assert active_days_from_desc("招聘中") is None


def test_numeric_fallback_handles_unlisted_combinations():
    assert active_days_from_desc("7日内活跃") == 7
    assert active_days_from_desc("6月内活跃") == 180


# ---- 单一真源 ----

def test_active_and_closed_tables_are_not_duplicated():
    """这两张表只许在 normalization.py 里定义一次。

    曾在三处各写一份并漂开：`今日活跃` 一处算 1 天一处算 0 天、
    `半年前活跃` 一处 999 一处 180。**后果是同一个岗位在采集链路和刷新链路
    得出不同的降级结论**，而降级是单向不可逆的。
    """
    owner = ROOT / "src" / "job_radar" / "normalization.py"
    # 判据是「同一行里出现两个以上文案」——那才是在抄表。
    # 单个文案的字面量是正常用法（SQL 里比较 job_status_desc 的列值、日志文案），
    # 不能一概禁掉，否则测试会逼着人把可读的 SQL 改成拼字符串。
    # 判据是「同一行里把文案映射到天数」。分值表（profile.ACT_SCORE，文案→0-1 权重）
    # 是另一个正交维度，不算重复；单个文案的字面量（SQL 比较列值、日志文案）也不算。
    probes = ("职位已关闭", "已下线", "职位已结束", "暂停招聘", "招聘已结束",
              "半年前活跃", "近半年活跃", "3日内活跃", "刚刚活跃")
    # 天数表写成 `"文案": 30`（整数），分值表写成 `"文案": 0.3`（小数）。
    mapping = re.compile(r"[\"'][^\"']*活跃[\"']\s*:\s*(\d+(?:\.\d+)?)")
    offenders: list[str] = []
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
        if path == owner or "_archive" in path.parts:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#")[0]
            if sum(p in code for p in probes) < 2:
                continue
            values = mapping.findall(code)
            if values and all("." not in v for v in values):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {line.strip()[:60]}")
    assert not offenders, (
        "这些地方又抄了一份文案→天数表，收敛到 normalization.py：\n  "
        + "\n  ".join(offenders))


def test_closed_markers_cover_the_platform_wordings():
    for phrase in ("职位已关闭", "职位已下线", "暂停招聘", "招聘已结束"):
        assert phrase in CLOSED_MARKERS
