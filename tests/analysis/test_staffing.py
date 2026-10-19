"""外包/人力服务公司名单的匹配规则。

这套规则的价值全在**拒绝误报**上：2~3 字的中文主体名做子串匹配一定误报，
实测已踩过 智联/宇信/东软/微创/海隆/宝信 六个坑。所以测试的重点不是"能命中多少"，
而是"这些坑必须一个都不踩"。
"""
from __future__ import annotations

import pytest

from job_radar.profile import (
    _STAFFING_SHORT,
    _STAFFING_STRONG,
    is_staffing_company,
    staffing_signal,
)


def test_list_is_loaded():
    """名单要真的读到 —— 文件路径写错会静默返回空集，等于这个功能不存在。"""
    assert len(_STAFFING_STRONG) > 150, f"强词只有 {len(_STAFFING_STRONG)} 条，疑似没读到名单"
    assert len(_STAFFING_SHORT) > 80, f"短词只有 {len(_STAFFING_SHORT)} 条"


@pytest.mark.parametrize("name", [
    "上海万宝盛华信息科技",       # 强词子串
    "外企德科数字",
    "北京软通动力云计算",
    "北京外企德科人力资源服务",   # 库内截断名
    "深圳中智",                   # 短词 + 去城市前缀后开头
    "浙江中智经济技术合作",
    "武汉佰钧成技术",
    "人瑞网络科技",               # 短词 + 本身开头
    "中智科创",
])
def test_staffing_companies_are_detected(name):
    assert is_staffing_company(name) is True


@pytest.mark.parametrize("name", [
    "华秋智联",                   # 撞「智联」，实为电子元器件分销商
    "厦门鹭港智联算力科技",       # 同上
    "海宇信息",                   # 撞「宇信」
    "山东软件科技",               # 去「山东」后是「软件科技」，撞不到「东软」
    "上海微创医疗器械",           # 撞「微创」，但名单只收「微创软件」
    "华中智能科技",               # 撞「中智」的前两个字但顺序不同
    "海隆石油",                   # 撞「海隆」
    "宝信汽车",                   # 撞「宝信」
    "上海起点计划科技",           # 撞「起点」，名单已改为全长「起点人力」
    "正德良品",                   # 撞「正德」，名单已改为全长「正德人力」
    "卡思科",                     # 撞「卡思」，名单已改为全长「卡思优派」
    "华为技术有限公司",
    "字节跳动",
    "阿里巴巴集团",
    "网易杭州",
])
def test_known_false_positives_are_rejected(name):
    """这些是实测过的误报坑，任何一个回归都会让筛选悄悄变差。"""
    assert is_staffing_company(name) is False


def test_empty_name_is_not_staffing():
    assert is_staffing_company("") is False
    assert is_staffing_company("   ") is False


def test_staffing_signal_has_three_orthogonal_paths():
    """名单 / 行业标签 / 平台代招，三路并联 —— 任何一路单独都漏一截。

    名单抓得到 ITO 大厂（行业标「计算机软件」），行业标签抓得到名单没收的小微
    人力公司，`is_proxy_job` 是平台自己标的代招。
    """
    assert staffing_signal({"company_name": "中软国际"}) == "公司名在人力服务名单内"
    assert staffing_signal({"company_name": "某科技公司",
                            "industry": "人力资源服务"}) == "行业标为人力资源服务"
    assert staffing_signal({"company_name": "某科技公司",
                            "is_proxy_job": 1}) == "平台标记为代招"
    assert staffing_signal({"company_name": "字节跳动"}) == ""
