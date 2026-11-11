"""维度候选召回测试（纯 Python，无外部依赖）。"""

from dmf_agent.recall.common_str import get_common_str_by_interval
from dmf_agent.recall.dimension import (
    DimensionRecaller,
    get_primary_name,
    is_substr,
)


# ---- get_common_str_by_interval ----


def test_common_str_exact_substring():
    assert get_common_str_by_interval("按渠道看GMV", "渠道") == "渠道"


def test_common_str_docstring_case():
    # 老系统 __main__ 示例：营业部 -> 营业（部不在 query 中）
    assert get_common_str_by_interval("查询2024年营业净收入环比增长的原因", "营业部") == "营业"


def test_common_str_interval_tolerant():
    # 新增用户数 vs 新增户数：中间隔了"用"，间隔容忍仍匹配
    assert get_common_str_by_interval("新增户数", "新增用户数") == "新增户数"


def test_common_str_single_char_dropped():
    assert get_common_str_by_interval("你好世界", "查") == ""


def test_common_str_no_match():
    assert get_common_str_by_interval("你好", "GMV") == ""


def test_common_str_single_multichar_token_kept():
    # 单个多字符 token（GMV）完整命中应保留（老实现字符/token 错位会漏配）
    assert get_common_str_by_interval("近7天GMV趋势", "GMV") == "GMV"


def test_common_str_interval_exceeded():
    # "收"与"入"之间隔了多个字仍<=3；构造间隔>3 的：query "新一二三四增" -> "新增" 间隔 4
    assert get_common_str_by_interval("新一二三四增", "新增") == ""


def test_common_str_first_query_token_matches_late_short_token():
    # 回归（2026-09-01 过滤召回引入）：query 首 token 匹配 short 非首 token
    # （i==1, j>1）时 DP 窗口 range(lo, i) 为空，修复前抛 ValueError
    result = get_common_str_by_interval("线上门店渠道", "渠道线上")
    assert len(result) == 2


# ---- get_primary_name ----


def test_primary_name_strips_parentheses():
    assert get_primary_name("渠道（一级）") == "渠道"
    assert get_primary_name("城市(新)") == "城市"
    assert get_primary_name("消费渠道") == "消费渠道"
    assert get_primary_name("") == ""


# ---- is_substr ----


def test_is_substr_entity_contains_dim():
    # 实体"消费渠道"包含候选名"渠道" -> 返回候选名
    assert is_substr(["消费渠道"], "渠道") == "渠道"


def test_is_substr_no_overlap():
    assert is_substr(["城市"], "渠道") is None


def test_is_substr_empty_entities():
    assert is_substr([], "渠道") is None


# ---- DimensionRecaller ----


def test_recall_exact_dim():
    recaller = DimensionRecaller()
    result = recaller.recall("按渠道看GMV", ["渠道", "城市", "支付方式"])
    assert result.dimensions[0] == "渠道"
    assert "城市" not in result.dimensions


def test_recall_fuzzy_dim():
    recaller = DimensionRecaller()
    result = recaller.recall("新增户数", ["新增用户数", "复购率", "城市"])
    assert "新增用户数" in result.dimensions
    assert "新增用户数" in result.string_mapping_high or "新增用户数" in result.exact_mapping


def test_recall_strips_parentheses():
    recaller = DimensionRecaller()
    result = recaller.recall("按渠道看", ["渠道（一级）", "城市"])
    assert result.dimensions[0] == "渠道（一级）"


def test_recall_entities_exact_mapping():
    recaller = DimensionRecaller()
    result = recaller.recall("各渠道的GMV", ["渠道"], entities=["渠道"])
    assert result.exact_mapping == ["渠道"]


def test_recall_empty_query():
    recaller = DimensionRecaller()
    result = recaller.recall("", ["渠道", "城市"])
    assert result.dimensions == []


def test_recall_single_char_dim_needs_entities():
    recaller = DimensionRecaller()
    result = recaller.recall("按月看", ["月", "城市"])
    # 单字维度名走完全匹配通道，无实体时 mapping 通道不召回
    assert result.dimensions == []


def test_recall_low_score_excluded():
    recaller = DimensionRecaller()
    # "一二三四五" 与 query 仅共享极少量字符，低于 dim_min_value=0.4
    result = recaller.recall("渠道", ["渠道", "一二三四五"])
    assert "一二三四五" not in result.dimensions
