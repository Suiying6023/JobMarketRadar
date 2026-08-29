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
    # 只断言"提取出了词"，不指定是哪几个 —— 上限只有 _MAX_TECH 个位置，
    # 断言某个特定词必须入选会和上限调整互相打架（实测踩到：上限从 6 降到 5
    # 后 LangChain 被挤掉，那是刻意取舍，不是缺陷）。
    assert len(out) == E._MAX_TECH
    assert "Python" in out and "Java" in out       # 第一句就出现，必然入选
    # 每个标签都必须短到能一眼读完，且不含句读 —— 这才是这个测试真正要守的
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


def test_four_column_layout_absorbs_spare_width_with_jd_peek():
    """四列：岗位信息 | LLM 标签 | JD 摘要 | 操作。

    左列必须有**宽度上限**，不能用 1fr 独占剩余空间：实测 1fr 下左列分到
    1014px，而真实内容最宽只有 481px、中位 302px —— 在岗位列和标签列之间
    留出 533px 空白，眼睛要横跨半个屏幕才能把一条岗位和它的标签对上。
    富余宽度由 JD 摘要列吸收（1fr），那比空着有用。
    """
    css = T.CSS
    # 断言结构性质而非整串字面量：像素值会随排版微调而变，
    # 会守的是「左列有上限、JD 列吸收富余、另两列定宽」这三件事。
    cols = css.split("grid-template-columns:", 1)[1].split(";")[0].strip()
    parts = cols.split()
    assert len(parts) == 4, f"应为四列，实际 {cols!r}"
    assert parts[0].startswith("minmax(0,") and parts[0].endswith("px)"), \
        f"左列要设上限，不能用 1fr 独占剩余空间：{parts[0]}"
    assert parts[1].endswith("px"), f"标签列要定宽，否则长标签会撑变形：{parts[1]}"
    assert parts[2] == "minmax(0,1fr)", f"JD 摘要列吸收富余宽度：{parts[2]}"
    assert parts[3].endswith("px"), f"操作列要定宽，位置才固定可盲点：{parts[3]}"
    # 按钮竖排：卡片高度由 JD 窗口固定（108px），竖排不再额外撑高，
    # 而横排时三个按钮挤在 128px 里每个只有 38px 宽、点击目标太小。
    ops = css.split(".ops{")[1].split("}")[0]
    assert "flex-direction:column" in ops
    assert "align-items:stretch" in ops, "按钮要占满整列宽，点击目标才够大"
    # 容器不设 max-width：那会让卡片居中留白而顶栏全宽，两者对不齐
    assert "main{padding:10px 16px 64px}" in css
    js = T.JS
    for act in ("jd", "ap", "dm"):
        assert f'data-act="{act}"' in js, f"缺少操作按钮 {act}"


def test_demote_has_no_confirm_dialog_but_stays_undoable():
    """降级不弹确认框，改用事后可撤。

    降级是高频动作（筛池子时一路往下刷），每次确认会让人养成盲点「确定」的
    习惯 —— 那时确认框只剩摩擦、不剩保护。安全网换成两处撤回入口：
    toast 上的按钮 + 顶栏常驻「恢复本页隐藏的 N 条」。
    """
    js = T.JS
    body = js.split("async function demote(r){")[1].split("\n}\n")[0]
    assert "confirm(" not in body, "降级不该弹确认框"
    # 撤回必须同时还原视图和数据库
    assert "'/restore'" in body or '"/restore"' in body, "撤回要让服务把它撤回 active"
    assert "HIDDEN.delete" in body, "撤回要恢复本页显示"
    # 实测过的 bug：只 render() 不 drawChips()，顶栏撤回入口不出现
    assert "drawChips()" in body, "降级后没重绘顶栏撤回入口"
    assert "undo" in body, "toast 要带撤回动作"


def test_toast_can_carry_an_action():
    """toast 要能承载撤回按钮 —— 它替代了确认框做安全网，不能只是一条消息。"""
    js = T.JS
    body = js.split("function toast(msg,bad,undo){")[1].split("\n}\n")[0]
    assert "tundo" in body and "onclick" in body
    # 带动作的 toast 要停留更久，否则来不及点
    assert "undo?6000:3200" in body


def test_jd_peek_scrolls_vertically_with_a_fixed_height():
    """JD 摘要纵向滚动，高度写死。

    单行只能显示约 73 字 = JD 中位长度（592 字）的 12%，4 行能到约 30%，
    多数岗位的核心职责就在这个范围里。高度写死让每张卡一样高（实测 108px），
    列表节奏稳定 —— 若让 JD 决定高度，长短不一会让滚动时失去参照。
    """
    peek = T.CSS.split(".peek{")[1].split("}")[0]
    assert "overflow-y:auto" in peek, "JD 要能纵向滚动读完"
    assert "height:78px" in peek, "高度写死，否则每张卡高度不一"
    assert "white-space:nowrap" not in peek, "已改纵向，不该再限制换行"
    # 窄屏优先牺牲它：它是"顺便看到"的信息，标签和操作是必需的
    assert ".peek{display:none}" in T.CSS


def test_left_column_uses_three_rows():
    """左列三行：岗位 / 岗位属性 / 公司属性。

    卡片高度由 JD 窗口决定（108px），左列只有两行时下方会空一片。
    拆出第三行既填上空间，又让"岗位属性"（决定投不投）和"公司属性"
    （决定值不值得投）分开。
    """
    assert ".r3{" in T.CSS
    js = T.JS
    assert 'class="r3"' in js
    # 公司背景字段要真的用上，否则第三行还是空的
    for f in ("r.size", "r.stage", "r.cap", "r.found", "r.regStatus"):
        assert f in js, f"第三行没用到 {f}"


def test_jd_peek_skips_boilerplate_headers():
    """JD 开头常是「岗位职责：」这类小标题，直接截会得到一屏标题没有内容。"""
    js = T.JS
    assert "_HDR" in js and "岗位职责" in js
    body = js.split("function peek(jd){")[1].split("\n}")[0]
    assert "filter" in body, "要过滤掉纯标题行"
    assert "body.length?body:lines" in body, "全是标题行时要退回原文，别返回空白"


def test_ties_are_broken_deterministically_not_by_sql_order():
    """同分必须有确定次序，否则榜首是谁取决于 SQL 返回顺序。

    实测：综合分只有约 76 个可达值（各维度都走 0.5 步长）而池子近 800 条，
    **99.2% 的岗位与他人同分，最大同分组 22 条**。JS 的 Array.sort 是稳定排序，
    所以同分组内的相对顺序就是数据数组的顺序 —— 与岗位质量完全无关。
    """
    js = T.JS
    body = js.split("rows.sort(")[1].split("return rows")[0]
    assert "techN" in body, "缺技术栈命中数作为二级键"
    assert "actDays" in body, "缺活跃天数作为三级键"
    assert "verified" in body, "缺最后核实日期作为四级键"


def test_record_carries_the_untruncated_tech_hit_count():
    """`techN` 必须是**未截断**的命中数，否则区分不了撞上限的那批。

    `job_score` 里技术栈 ×0.5 后撞 `W_TECH_MAX`，实测 17.3% 顶在上限上，
    命中 6 个词和 12 个词总分相同 —— 二级键取截断后的值就等于没有二级键。
    """
    recs, _ = E.collect()
    assert recs, "投递池为空，无法验证"
    assert all(isinstance(r["techN"], int) for r in recs[:50])
    # 必须有超过上限对应命中数（W_TECH_MAX/0.5）的记录，否则证明不了它未截断
    cap_hits = int(L.P.W_TECH_MAX / 0.5)
    assert any(r["techN"] > cap_hits for r in recs), \
        f"没有 techN > {cap_hits} 的记录，无法确认它未被截断"
    # 与 job_score 同源：不能在两处各写一套匹配逻辑
    assert hasattr(L, "tech_hits")


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
