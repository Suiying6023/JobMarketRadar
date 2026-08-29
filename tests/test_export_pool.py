"""单页投递池导出测试。

`export_pool.py` 取代了七文件方案（`export_pool_html.py`）。这里钉死两件事：
① 判定口径**复用**旧模块，不是另写一套 —— 两份阈值会各自漂移，
   而漂移后无法判断页面上看到的是哪一套规则的结果；
② 分桶从"文件边界"降级成"字段"后，每条记录必须带齐 bucket/tier/risk，
   否则前端筛不出对应视图，症状是某个标签永远 0 条（不报错）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import _pool_template as T  # noqa: E402
import export_pool as E  # noqa: E402
import export_pool_html as L  # noqa: E402


def test_reuses_the_legacy_predicates_instead_of_redefining_them():
    """评分/硬过滤/分桶/分层必须是旧模块那几个函数本身。"""
    assert E.L is L
    for name in ("job_score", "hard_drop", "salary_bucket", "active_tier",
                 "real_risk_tags", "salary_mid", "last_verified", "observed_days_ago"):
        assert hasattr(L, name), f"缺少复用目标 {name}"


def test_record_carries_bucket_tier_and_risk():
    """分桶信息必须在记录里，前端才能把它当筛选条件。

    漏了不会报错 —— 只是对应视图永远 0 条，而空列表看起来和"确实没有"一样。
    """
    recs, _ = E.collect()
    assert recs, "投递池为空，无法验证（先跑一次采集与导出）"
    for r in recs[:50]:
        assert r["bucket"] in ("core", "high", "low", "over")
        assert r["tier"] in ("", "p0", "p1", "p2")
        assert isinstance(r["risk"], list)
    # core 桶必须带分层，非 core 不该带（分层只对核心薪资档有意义）
    for r in recs:
        if r["bucket"] == "core":
            assert r["tier"], "core 桶缺少活跃度分层"
        else:
            assert not r["tier"], f"{r['bucket']} 桶不该有分层"


def test_every_view_in_template_is_reachable():
    """模板里定义的每个视图都要有记录能落进去，否则那个标签是死的。

    唯一允许为空的是 p1（平台未返回活跃度的岗位，本来就少）。
    """
    recs, _ = E.collect()
    counts = {
        "p0": sum(1 for r in recs if r["bucket"] == "core" and r["tier"] == "p0"),
        "p2": sum(1 for r in recs if r["bucket"] == "core" and r["tier"] == "p2"),
        "high": sum(1 for r in recs if r["bucket"] == "high"),
        "low": sum(1 for r in recs if r["bucket"] == "low"),
        "over": sum(1 for r in recs if r["bucket"] == "over"),
        "risk": sum(1 for r in recs if r["risk"]),
    }
    empty = [k for k, v in counts.items() if v == 0]
    assert not empty, f"这些视图没有任何记录: {empty}"


def test_noise_values_never_reach_the_page():
    """「未提及」和求职者已满足的条件不该占标签位。

    实测：工作制字段 956 条是「未提及」（LLM 对"JD 没写"的正常输出），
    gates 里 506 条是「本科及以上」「经验不限」—— 求职者是统招全日制本科
    应届生，这些他已满足。两者加起来让页面满屏零信息量标签，
    把真正该看的经验年限门槛淹掉。
    """
    assert E.is_noise("未提及")
    assert E.is_noise("本科及以上")
    assert E.is_noise("统招本科及以上学历")
    assert E.is_noise("经验不限")
    assert E.is_noise("学历不限")
    assert E.is_noise("")
    # 真门槛不能被当噪音滤掉
    assert not E.is_noise("硕士及以上")
    assert not E.is_noise("985/211")
    assert not E.is_noise("3年以上开发经验")
    assert not E.is_noise("双休")


def test_real_gates_keeps_only_blockers():
    """gates 只留会挡人的：经验年限、名校、硕博。"""
    g = {"exp_req": ["3年以上后端开发经验", "经验不限"],
         "edu_req": ["本科及以上学历", "硕士及以上"],
         "school_req": ["985/211"], "other_req": ["未提及"]}
    out = E.real_gates(g)
    assert any("3年以上" in x for x in out)
    assert any("硕士" in x for x in out)
    assert any("985" in x for x in out)
    assert not any("本科及以上" in x for x in out)
    assert not any("经验不限" in x for x in out)
    assert not any("未提及" in x for x in out)


def test_tech_tags_are_extracted_words_not_truncated_sentences():
    """技术栈必须是**提取出的短词**，不是截断的整句。

    实测 tech_req 的值 28% 超过 18 字（中位 10、最长 250），截断会产出
    「深度学习框架（如TensorFlow」这种断句 —— 读不懂且长短不齐，
    标签列没法纵向扫，而"能扫一列"正是把标签独立成列的目的。
    """
    out = E.dedupe_tech([
        "熟练掌握 Python、Java 或其他编程语言进行测试脚本或自动化测试开发",
        "理解大模型应用的基本原理，了解 Prompt、RAG、Function Calling",
        "LangChain、LangGraph等主流Agent开发框架",
    ])
    assert "Python" in out and "Java" in out
    assert "RAG" in out and "LangChain" in out
    # 每个标签都必须短到能一眼读完，且不含句读
    for t in out:
        assert len(t) <= 18, f"{t!r} 太长，说明是句子不是词"
        assert not any(c in t for c in "，。、（）"), f"{t!r} 含句读，是截断的句子"


def test_tech_vocabulary_covers_the_profile_scoring_words():
    """展示用词表必须含画像评分用的那 27 个词 —— 页面上看到的技术栈
    应当和评分依据一致，否则"分数高但标签看不出为什么"。"""
    import job_radar.profile as P
    vocab = {t.lower() for t in E._TECH_VOCAB}
    missing = [t for t in P.AI_TECH if t.lower() not in vocab]
    assert not missing, f"词表缺少画像评分词: {missing}"


def test_jd_text_is_included_because_gates_live_there():
    """JD 正文必须带上：所有硬门槛（经验年限、名校、双休、外包）都在正文里，
    接口字段只能粗筛。它占 65% 体积，但砍掉就等于砍掉页面的核心价值。"""
    recs, _ = E.collect()
    with_jd = sum(1 for r in recs if r["jd"])
    assert with_jd == len(recs), "有记录缺 JD 正文"
    assert any(len(r["jd"]) > 200 for r in recs)


def test_page_is_self_contained_single_file():
    """数据内嵌而非 fetch 外部 JSON —— 页面要能直接双击（file://）打开，
    那种情况下 fetch 会被 CORS 挡掉。"""
    html = T.build('[{"id":"x"}]', '{"date":"2026-01-01"}', '{"staleFull":30}')
    assert html.startswith("<!DOCTYPE html")
    assert "const DATA=" in html and "const T=" in html
    assert "fetch(" not in html.split("markApplied")[0], "数据不该靠 fetch 载入"
    # 单文件：不引用任何外部资源
    for bad in ("<link", "src=\"http", "@import"):
        assert bad not in html, f"页面引用了外部资源: {bad}"


def test_gate_labels_are_short_enough_for_a_fixed_width_column():
    """门槛必须压成短标签，不能铺整句。

    实测原始 gates 值中位 14 字、89 条超 20 字、最长 40 字（LLM 抽的是整句）。
    整句会把固定宽的标签列撑变形，而句子里真正是门槛的只有开头几个字：
    「1-3年真实Python开发经验，有内部业务系统…」的门槛就是「1-3年」。
    """
    assert E.gate_label("1-3年真实Python开发经验，有内部业务系统交付经历") == "1-3年经验"
    assert E.gate_label("本科 3 年以上相关工作经验，硕士 1 年以上") == "硕士"
    assert E.gate_label("统招985/211重点院校毕业生优先") == "985/211"
    assert E.gate_label("熟悉分布式系统设计") is None      # 不是门槛
    for v in ("2年以上开发经验", "5年以上产品经理或解决方案经验，有至少 10 个案例"):
        lab = E.gate_label(v)
        assert lab and len(lab) <= 10, f"{v!r} → {lab!r} 太长"


def test_tech_tag_count_fits_one_line():
    """标签数上限要让绝大多数条目只占一行。

    实测 340px 列宽下：6 个标签 31% 换行、5 个 11%、4 个近 0。
    换行会把卡片从 ~44px 撑到 64px，一屏少看三四条。取 5 是这个权衡的结果。
    """
    assert E._MAX_TECH == 5
    many = ["Python", "Java", "Go", "React", "Vue", "Redis", "Kafka", "Docker"]
    assert len(E.dedupe_tech(many)) <= 5


def test_three_column_layout_with_ops_column():
    """三列：岗位信息 | LLM 标签 | 操作。

    左列必须有**宽度上限**，不能用 1fr 独占剩余空间：实测 1fr 下左列分到
    1014px，而真实内容最宽只有 481px、中位 302px —— 在岗位列和标签列之间
    留出 533px 空白，眼睛要横跨半个屏幕才能把一条岗位和它的标签对上。
    富余宽度要由**最后一列**吸收，空白留在最右边的页边距里。
    """
    css = T.CSS
    assert "grid-template-columns:minmax(0,560px) 340px minmax(122px,1fr)" in css, \
        "左列要设上限、末列吸收富余；标签列固定宽否则长标签会把它撑变形"
    assert ".ops{display:flex;gap:4px" in css, "操作按钮横排（竖排会把卡撑高一倍）"
    # 容器不设 max-width：那会让卡片居中留白而顶栏全宽，两者对不齐
    assert "main{padding:10px 16px 64px}" in css
    js = T.JS
    for act in ("jd", "ap", "dm"):
        assert f'data-act="{act}"' in js, f"缺少操作按钮 {act}"


def test_demote_redraws_the_undo_entry():
    """降级后必须重绘 chips —— 撤回入口是按 HIDDEN.size 生成的。

    实测过的 bug：只 render() 不 drawChips()，误点降级后没有任何撤回入口，
    而降级已经写进数据库了。
    """
    js = T.JS
    demote_body = js.split("async function demote(r){")[1].split("\n}")[0]
    assert "drawChips()" in demote_body, "降级后没重绘撤回入口"
    assert "confirm(" in demote_body, "降级是写库操作，要先确认"


def test_tab_counts_use_per_view_exclusions():
    """标签计数必须按各自视图的排除规则算。

    实测过的 bug：计数统一用"当前视图"的排除集合，于是「风险公司」标签显示 0
    （它数的是已把风险过滤掉的集合），「全部」的计数也和点进去看到的不一致。
    """
    js = T.JS
    assert "function pool(vk)" in js, "pool 必须接收目标视图"
    assert "pool(v.k)" in js, "标签计数要按各自视图算，不能用当前视图"


def test_applied_state_falls_back_to_local_storage_offline():
    """跑服务时写库（POST /apply），file:// 打开时退化到 localStorage。
    两条路都要有：页面既是静态文件，跑服务时又不该丢状态。"""
    js = T.JS
    assert "localStorage" in js
    assert "/apply" in js
    assert "LIVE" in js, "要区分 http 与 file 协议"
    # 接口失败必须可见：静默失败会让人以为已入库，重导出后标记消失且无从溯源
    assert "toast(" in js
