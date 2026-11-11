"""指标阈值过滤（FR1）：中文比较条件解析 -> DSL -> 编译器外层 WHERE（HAVING 语义）。"""

from dmf_agent.agents.metric_agent.nodes.build_dsl import _normalize_metric_filters
from dmf_agent.agents.metric_agent.threshold import parse_metric_filters
from dmf_agent.config.settings import Settings
from dmf_agent.semantic import MetricFlowSqlCompiler, SemanticLayer
from dmf_agent.semantic.query import MetricQuery

layer = SemanticLayer()
compiler = MetricFlowSqlCompiler(layer, Settings().starrocks)


def test_parse_prefix_operators_and_units():
    assert parse_metric_filters("GMV大于100万的门店", ["GMV"]) == [
        {"metric": "GMV", "operator": ">", "value": 1_000_000.0}
    ]
    assert parse_metric_filters("销售额超过5000", ["销售额"]) == [
        {"metric": "销售额", "operator": ">", "value": 5000.0}
    ]
    assert parse_metric_filters("不良贷款率低于1", ["不良贷款率"]) == [
        {"metric": "不良贷款率", "operator": "<", "value": 1.0}
    ]


def test_parse_inclusive_and_suffix():
    assert parse_metric_filters("GMV不低于3000", ["GMV"])[0]["operator"] == ">="
    assert parse_metric_filters("GMV100万以上的门店", ["GMV"]) == [
        {"metric": "GMV", "operator": ">=", "value": 1_000_000.0}
    ]
    assert parse_metric_filters("GMV500以下", ["GMV"])[0]["operator"] == "<="


def test_parse_yi_unit_and_multi_metric():
    res = parse_metric_filters("GMV大于1.2亿", ["GMV"])
    assert res[0]["value"] == 120_000_000.0
    # 条件归属最近指标
    res = parse_metric_filters("GMV大于100万且订单数小于50", ["GMV", "订单数"])
    ops = {(r["metric"], r["operator"]): r["value"] for r in res}
    assert ops[("GMV", ">")] == 1_000_000.0
    assert ops[("订单数", "<")] == 50.0


def test_parse_no_condition_or_no_metric():
    assert parse_metric_filters("近7天GMV按渠道", ["GMV"]) == []
    assert parse_metric_filters("GMV大于100万", []) == []


def test_normalize_metric_filters_dedup_and_validate():
    items = [
        {"metric": "GMV", "operator": ">", "value": "100"},
        {"metric": "GMV", "operator": ">", "value": 200},   # 同指标同算子去重
        {"metric": "GMV", "operator": "~~", "value": 1},    # 非法算子丢弃
        {"metric": "GMV", "operator": ">=", "value": "x"},  # 非法值丢弃
    ]
    out = _normalize_metric_filters(items, layer)
    assert out == [{"metric": "GMV", "operator": ">", "value": 100.0}]


def test_compile_metric_threshold_to_outer_where():
    query = MetricQuery.from_dsl(
        {
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
            "metric_filters": [{"metric": "GMV", "operator": ">", "value": 1000000}],
        },
        layer,
    )
    assert query.metric_ops["GMV"].filter_operator == ">"
    assert query.metric_ops["GMV"].filter_value == 1000000
    sql = compiler.compile(query).sql
    # 阈值下推为外层 WHERE（聚合列过滤，等价 HAVING）；数值经 :g 格式化（1e+06）
    assert "WHERE `GMV` >" in sql
