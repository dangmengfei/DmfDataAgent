"""排序 / TopN 解析测试（FR1 六要素之排序，纯函数）。"""

from dmf_agent.agents.metric_agent.sorting import (
    normalize_llm_order,
    parse_limit,
    parse_sort,
)


# ---- TopN 数量 ----


def test_parse_limit_variants():
    assert parse_limit("取前3的门店") == 3
    assert parse_limit("top5渠道") == 5
    assert parse_limit("TOP 10") == 10
    assert parse_limit("第2的门店") == 2
    assert parse_limit("排名前3名") == 3
    assert parse_limit("10强门店") == 10


def test_parse_limit_time_expressions_excluded():
    # 时间量词不是 TopN
    assert parse_limit("近7天GMV") is None
    assert parse_limit("前7天的GMV") is None
    assert parse_limit("第3季度销售额") is None
    assert parse_limit("近3个月GMV") is None
    assert parse_limit("最低的5个门店") == 5
    assert parse_limit("各门店的GMV") is None


# ---- parse_sort ----


def test_parse_sort_topn_desc_single_metric():
    spec = parse_sort("取GMV前10的门店", ["GMV"], ["门店"])
    assert spec is not None
    assert spec.metric == "GMV"
    assert spec.direction == "desc"
    assert spec.limit == 10
    assert spec.order_by == "GMV desc"


def test_parse_sort_asc_min():
    spec = parse_sort("GMV最低的5个门店", ["GMV"], ["门店"])
    assert spec is not None
    assert spec.direction == "asc"
    assert spec.limit == 5
    assert spec.order_by == "GMV asc"


def test_parse_sort_explicit_direction_without_limit():
    spec = parse_sort("各门店GMV从高到低", ["GMV"], ["门店"])
    assert spec is not None
    assert spec.direction == "desc"
    assert spec.limit is None

    asc = parse_sort("按GMV升序排列", ["GMV"])
    assert asc is not None and asc.direction == "asc"


def test_parse_sort_no_signal_returns_none():
    assert parse_sort("查一下近7天各渠道的GMV", ["GMV"], ["渠道"]) is None
    assert parse_sort("", ["GMV"]) is None


def test_parse_sort_metric_must_be_determinable():
    # 单指标直接采用
    spec = parse_sort("前5", ["GMV"])
    assert spec is not None and spec.metric == "GMV" and spec.limit == 5
    # 多指标但无任何线索 -> 排序列留空（仅 limit 生效，默认排序兜底）
    spec = parse_sort("GMV和订单数前5", ["GMV", "订单数"])
    assert spec is not None and spec.limit == 5
    assert spec.metric in ("GMV", "订单数")


# ---- LLM 自由文本归一 ----


def test_normalize_llm_order_canonical():
    assert normalize_llm_order("GMV desc", ["GMV"]) == ("GMV desc", None)
    assert normalize_llm_order("GMV asc", ["GMV"]) == ("GMV asc", None)


def test_normalize_llm_order_chinese_and_limit():
    order, limit = normalize_llm_order("按GMV从高到低 前5", ["GMV"])
    assert order == "GMV desc"
    assert limit == 5
    order, limit = normalize_llm_order("销售额 降序", ["销售额"])
    assert order == "销售额 desc"
    assert limit is None


def test_normalize_llm_order_unknown_column_rejected():
    assert normalize_llm_order("按金额降序", ["GMV"]) == (None, None)
    assert normalize_llm_order(None, ["GMV"]) == (None, None)
    assert normalize_llm_order("", ["GMV"]) == (None, None)
