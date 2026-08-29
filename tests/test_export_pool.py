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
