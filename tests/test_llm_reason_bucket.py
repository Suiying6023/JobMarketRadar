"""LLM 降级原因的归类测试。

这份统计唯一的用途是回答「哪类门槛卡掉最多岗位」，所以**桶的相对大小就是
它的全部价值** —— 归类错位不会报错，只会让人据此做出错误的参数决策。

历史上踩过的两个坑（都让届别限制虚高六倍）：
① 自由文本直接当分类标签用 → 319 条降级打出 200 多行几乎不重复的输出；
② 归类靠措辞反推，而**同一个短语可能是另一类的标准结尾句式**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import apply_glm_filters as A  # noqa: E402


def b(reason: str) -> str:
    return A.llm_reason_bucket(reason)


def test_cited_clause_number_takes_priority_over_wording():
    """模型自己写的「命中第 N 条」是显式声明，比措辞反推可靠。

    实测用它做主判据后，兜底桶从 20 条（2.4%）降到 2 条（0.2%）。
    """
    assert b("逐条核对：命中第 2 条名校要求") == "LLM:名校门槛"
    assert b("命中排除项第 8 条：本岗位为外包驻场") == "LLM:外包驻场"
    # 编号优先于措辞：这句的措辞会命中「硕士以上」，但编号说的是第 1 条
    assert b("硕士优先但非硬性；真正的问题是命中第 1 条，要求 3 年以上经验") \
        == "LLM:经验年限"
    # 否定形式不被当成编号命中（落回措辞 fallback，而不是误报第 3 条）
    assert A._CLAUSE_RE.search("未命中第 3 条硕士要求") is None


def test_chinese_numeral_years_land_in_experience_bucket():
    """⚠️ 年限必须认中文数字，否则滑到届别限制里。

    原正则只写 `\\d`，于是「一年以上」「两年以上」「半年以上」全部漏掉，
    而这些理由的结尾通常是「…命中第1条不招应届」—— 于是被「届别限制」
    的 `不招应届` 模式接走。实测 21 条因此错位。
    """
    for s in ("JD 要求「一年以上的亚马逊产品开发经验」，命中第1条不招应届",
              "JD 明确要求「两年以上工作经验」",
              "至少半年以上同岗位经验，应届生不满足",
              "要求三年以上后端开发经验"):
        assert b(s) == "LLM:经验年限", f"{s!r} 应归经验年限，实际 {b(s)}"


def test_not_hiring_fresh_grads_phrase_does_not_steal_from_experience():
    """「不招应届」是经验年限论证的**标准结尾句式**，不能算届别限制。

    这是归类错位的根源：模型判经验门槛时几乎必然写
    「…命中第1条不招应届」，而旧的届别限制模式含 `不招应届`，
    于是把上一档的条目大批偷走（97 条里 80 条其实是经验年限）。
    """
    s = "JD 硬性要求「2 年及以上软件测试经验」，应届生无正式工作经验，命中第 1 条不招应届"
    assert b(s) == "LLM:经验年限"
    # 真届别限制要有具体届数或实习字样
    assert b("JD 标题明确写「27届华为校招」") == "LLM:届别限制"
    assert b("毕业时间须在2026年11月-2027年10月") == "LLM:届别限制"
    assert b("该岗位只招募实习生") == "LLM:届别限制"


def test_direction_mismatch_is_last_so_it_cannot_eat_earlier_buckets():
    """「方向不符」措辞最泛，必须排最后。

    实测把它提到最前会立刻误吃 106 条 —— 其中 71 条是经验年限、
    22 条是届别限制。这条顺序是有代价的，值得钉死。
    """
    labels = [name for name, _ in A.LLM_REASON_PATTERNS]
    assert labels[-1] == "方向不符", f"方向不符必须在最后，实际顺序 {labels}"
    assert labels[0] == "经验年限", "经验年限最常见，放最前"
    # 同时含两类措辞时，应归到更具体的那个
    s = "岗位为测试岗不属于编程开发岗；且硬性要求 3 年以上经验"
    assert b(s) == "LLM:经验年限"


def test_business_role_words_do_not_fall_into_the_catch_all():
    """非编程业务岗的词要能归类，否则落进兜底桶。

    实测兜底桶里 5 条是「灯具商品开发/文创产品开发/亚马逊选品/面料开发/
    海运进口操作」—— 这些词 NON_SW_ROLE_KW 里已经有了，归类模式却漏了。
    """
    for s in ("岗位实为灯具商品开发，非软件编程",
              "亚马逊选品与运营方向", "面料开发岗位", "文创产品开发"):
        assert b(s) == "LLM:方向不符", f"{s!r} 落进了 {b(s)}"


def test_every_bucket_name_is_reachable():
    """每个桶都要有能落进去的输入，否则那个标签是死的。"""
    names = {name for name, _ in A.LLM_REASON_PATTERNS}
    samples = {
        "经验年限": "要求 3 年以上经验",
        "届别限制": "仅限 27 届",
        "名校门槛": "要求 985/211 院校",
        "硕士以上": "硕士及以上学历",
        "非双休": "大小周工作制",
        "外包驻场": "本岗位为外包驻场",
        "英语/职称": "英语六级以上",
        "JD信息不足": "JD 信息不足无法判断",
        "方向不符": "岗位为纯运营方向",
    }
    assert names == set(samples), f"桶名与样例不匹配：{names ^ set(samples)}"
    for label, s in samples.items():
        assert b(s) == f"LLM:{label}", f"{s!r} → {b(s)}，期望 LLM:{label}"
