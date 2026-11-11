"""FR1 节点级测试：规则要素抽取产出排序/TopN/置信度 + 低置信触发澄清。"""

from dmf_agent.agents.metric_agent.nodes.build_dsl import build_build_dsl_node
from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
from dmf_agent.agents.metric_agent.nodes.validate_elements import (
    build_validate_elements_node,
)

from fakes import make_deps


async def test_rule_extract_topn_order_and_limit():
    deps = make_deps()
    elements = await _rule_based_extract("取GMV前10的门店", deps)
    assert "GMV" in elements["metrics"]
    assert "门店" in elements["dimensions"]
    assert elements["order_by"] == "GMV desc"
    assert elements["limit"] == 10
    assert elements["confidence"] >= 0.9


async def test_rule_extract_asc_min():
    deps = make_deps()
    elements = await _rule_based_extract("GMV最低的5个门店", deps)
    assert elements["order_by"] == "GMV asc"
    assert elements["limit"] == 5


async def test_rule_extract_plain_query_no_order():
    deps = make_deps()
    elements = await _rule_based_extract("近7天GMV按渠道", deps)
    assert elements["order_by"] is None
    assert elements["limit"] is None
    assert elements["confidence"] >= 0.9


async def test_validate_low_confidence_triggers_clarification():
    deps = make_deps()
    node = build_validate_elements_node(deps)
    result = await node(
        {"query_elements": {"metrics": ["GMV"], "dimensions": [], "confidence": 0.4}}
    )
    assert result["validation"]["ok"] is False
    assert any("置信度" in p for p in result["validation"]["problems"])


async def test_validate_high_confidence_passes():
    deps = make_deps()
    node = build_validate_elements_node(deps)
    result = await node(
        {"query_elements": {"metrics": ["GMV"], "dimensions": [], "confidence": 0.9}}
    )
    assert result["validation"]["ok"] is True
    assert result["validation"]["problems"] == []


# ---- 同环比短语增强 ----


async def test_rule_extract_pop_phrases():
    deps = make_deps()
    mom = await _rule_based_extract("GMV与上月比怎么样", deps)
    assert mom["period_comparison"] is True and mom["period_comparison_type"] == "mom"
    yoy = await _rule_based_extract("GMV较去年同期", deps)
    assert yoy["period_comparison_type"] == "yoy"
    qoq = await _rule_based_extract("GMV较上季度", deps)
    assert qoq["period_comparison_type"] == "qoq"
    plain = await _rule_based_extract("近7天GMV按渠道", deps)
    assert plain["period_comparison"] is False and plain["period_comparison_type"] is None


# ---- 指标阈值过滤 ----


async def test_rule_extract_metric_threshold():
    deps = make_deps()
    elements = await _rule_based_extract("GMV大于100万的门店", deps)
    assert elements["metric_filters"] == [
        {"metric": "GMV", "operator": ">", "value": 1_000_000.0}
    ]
    dsl = (await build_build_dsl_node(deps)({"query_elements": elements}))["dsl"]
    assert dsl["metric_filters"][0]["value"] == 1_000_000.0


# ---- 置信度阈值可配置 ----


async def test_low_confidence_threshold_configurable():
    from dmf_agent.config.settings import AgentSettings, Settings

    deps = make_deps(settings=Settings(agent=AgentSettings(low_confidence_threshold=0.3)))
    node = build_validate_elements_node(deps)
    # 0.4 在默认 0.5 下应澄清，但阈值降到 0.3 后放行
    result = await node({"query_elements": {"metrics": ["GMV"], "confidence": 0.4}})
    assert result["validation"]["ok"] is True
