"""分布分析（FR2）测试：Python 侧确定性分桶 + analyze 节点接线。"""

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent.nodes.analyze_data import build_analyze_data_node
from dmf_agent.analysis import compute_distribution, format_distribution

from fakes import make_deps


def _result(values, col="GMV"):
    return {
        "columns": ["门店", col],
        "rows": [[f"店{i}", v] for i, v in enumerate(values)],
    }


# ---- 纯函数 ----


def test_distribution_even_bins_cover_all():
    dist = compute_distribution(_result([10, 20, 30, 40, 50, 60, 70, 80]), "GMV", bins=4)
    assert dist is not None
    assert dist["metric"] == "GMV"
    assert dist["bin_count"] == 4
    assert dist["total_count"] == 8
    assert sum(b["count"] for b in dist["bins"]) == 8  # 全部落桶
    # 末桶闭区间含 max
    assert dist["bins"][-1]["hi"] == dist["max"] == 80
    assert dist["bins"][0]["lo"] == dist["min"] == 10


def test_distribution_single_value_and_insufficient_rows():
    # 全同值 -> 单桶 "="
    dist = compute_distribution(_result([5, 5, 5, 5]), "GMV")
    assert dist is not None and dist["bin_count"] == 1
    assert dist["bins"][0]["count"] == 4
    # 行数不足 3 -> None
    assert compute_distribution(_result([1, 2]), "GMV") is None


def test_distribution_no_numeric_or_missing_metric():
    # 非数值列 -> None
    assert compute_distribution({"columns": ["门店"], "rows": [["a"], ["b"], ["c"]]}) is None
    # 指定列名不存在 -> 回退首个数值列
    dist = compute_distribution(_result([1, 2, 3, 4]), "不存在指标")
    assert dist is not None and dist["metric"] == "GMV"
    # 空结果 -> None
    assert compute_distribution({"columns": [], "rows": []}) is None


def test_format_distribution_text():
    dist = compute_distribution(_result([10, 20, 30, 40]), "GMV", bins=2)
    text = format_distribution(dist)
    assert "GMV 分布" in text
    assert "4 个取值" in text


def test_fmt_value_units():
    from dmf_agent.analysis.distribution import _fmt_value
    assert _fmt_value(1500000) == "150万"
    assert _fmt_value(120000000) == "1.2亿"
    assert _fmt_value(1500) == "1.5k"
    assert _fmt_value(42) == "42"


# ---- analyze 节点接线 ----


async def test_analyze_distribution_keyword_produces_distribution():
    """问句含「分布」-> 分析模型工具库产出真实分桶（analysis_distribution，非编造）。

    2026-11-18 P1：「分布」从顶层 distribution 键迁到 analysis.results[]（行为一致，
    通道变化）；同时断言 `DMF_ANALYSIS-ENABLED=false` 时旧通道仍在（回归开关）。
    """
    deps = make_deps()
    node = build_analyze_data_node(deps)
    state = {
        "messages": [HumanMessage(content="各门店GMV的分布怎么样")],
        "query_elements": {"metrics": ["GMV"], "dimensions": ["门店"]},
        "dsl": {"metrics": [{"name": "GMV"}], "dimensions": ["门店"]},
        "query_result": _result([100, 200, 300, 400, 500, 600, 700, 800]),
        "skills": [],
        "intent": "metric_query",
    }
    result = await node(state)
    analysis = result["analysis"]
    results = analysis.get("results") or []
    assert results and results[0]["model"] == "analysis_distribution" and results[0]["ok"]
    dist = results[0]["data"]
    assert dist["metric"] == "GMV"
    assert sum(b["count"] for b in dist["bins"]) == 8
    # mock 模板摘要包含分布文字
    assert "GMV 分布" in analysis["summary"]

    # 回归开关：ENABLED=false 时旧顶层通道逐字保留
    deps.settings.analysis.enabled = False
    node2 = build_analyze_data_node(deps)
    result2 = await node2(dict(state))
    dist2 = result2["analysis"].get("distribution")
    assert dist2 is not None and dist2["metric"] == "GMV"
    assert not result2["analysis"].get("results")


async def test_analyze_plain_query_no_distribution():
    """普通问句不产出 distribution（不该对每次查询都做分桶）。"""
    deps = make_deps()
    node = build_analyze_data_node(deps)
    state = {
        "messages": [HumanMessage(content="近7天GMV趋势")],
        "query_elements": {"metrics": ["GMV"], "dimensions": []},
        "dsl": {"metrics": [{"name": "GMV"}], "dimensions": []},
        "query_result": _result([100, 200, 300]),
        "skills": [],
        "intent": "metric_query",
    }
    result = await node(state)
    assert "distribution" not in result["analysis"] or result["analysis"]["distribution"] is None
