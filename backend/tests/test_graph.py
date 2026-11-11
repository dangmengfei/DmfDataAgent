"""LangGraph 主图集成测试（无 LLM、无 StarRocks：规则降级 + 假语义引擎）。

主流程已脱离指标平台：元信息来自语义层（manifest.yml），取数以
语义查询计划 -> MetricFlow 编译 -> StarRocks 执行。本测试用
`tests/fakes.py::FakeMetricEngine` 顶替真实 StarRocks，覆盖图拓扑与状态流转。
"""

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
from fakes import make_deps


def _deps() -> AgentDeps:
    return make_deps()


async def test_metric_query_full_flow():
    """指标查询主链路：意图 -> 要素 -> 校验 -> DSL -> 取数 -> 分析 -> 答复。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="查一下近7天的GMV趋势")],
            "session_id": "t1",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "metric_query"
    assert result["query_elements"]["metrics"] == ["GMV"]
    assert result["validation"]["ok"] is True
    # DSL v3：指标以语义层标准名承载，物理表/列由语义清单编译
    assert result["dsl"]["version"] == "3.0"
    assert result["dsl"]["engine"] == "metricflow"
    assert result["dsl"]["metrics"][0]["name"] == "GMV"
    # 主流程无 mock 分支：取数结果带 SQL / 引擎 / 行数
    assert result["query_result"]["mock"] is False
    assert result["query_result"]["engine"] == "metricflow"
    assert result["query_result"]["row_count"] == 3
    assert result["analysis"]["stats"]["GMV"]["avg"] > 0
    assert "GMV" in result["final_answer"]
    # 时间解析接入：近7天 -> 范围不含今天（2026-08-30 起接入 time 模块）
    tr = result["query_elements"]["time_range"]
    assert tr["granularity"] == "day"
    assert tr["start"] < tr["end"]


async def test_clarify_flow():
    """要素缺失时走追问分支。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="统计一下情况怎么样")],
            "session_id": "t2",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "metric_query"
    assert result["validation"]["ok"] is False
    assert result["validation"]["problems"] == ["未能从问题中识别出任何业务指标"]
    assert "补充" in result["final_answer"]


async def test_chitchat_flow():
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="你好呀")],
            "session_id": "t3",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "chitchat"
    assert result["final_answer"]


async def test_knowledge_qa_flow_without_rag():
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="什么是北极星指标，文档里有说明吗")],
            "session_id": "t4",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "knowledge_qa"
    assert result["retrieved_docs"] == []
    assert "知识库" in result["final_answer"]


async def test_diagnosis_intent_flow():
    """归因意图（迁移自老系统 D 插件）：走取数链路，intent 落 trace。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="近7天GMV为什么下降了，帮我做个归因分析")],
            "session_id": "t5",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "diagnosis"
    # 归因同样先走取数链路（等价老系统"单一 D 插件自动补前置 A"）
    assert result["query_elements"]["metrics"] == ["GMV"]
    assert result["query_result"]["mock"] is False
    assert result["diagnosis"]["ok"] is True
    assert "归因" in result["final_answer"]


async def test_dimension_query_intent_flow():
    """维度查询意图（迁移自老系统 F 插件）：已识别维度但缺指标 -> 追问分支。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="北京有哪些门店")],
            "session_id": "t6",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "dimension_query"
    # 维度名可被语义层识别，但缺少指标 -> 追问（维度查询需依附某个指标）
    assert result["query_elements"]["dimensions"] == ["门店"]
    assert result["validation"]["ok"] is False
    assert result["validation"]["problems"] == ["未能从问题中识别出任何业务指标"]
    assert "补充" in result["final_answer"]


async def test_intent_with_skills_and_multi_query():
    """追加技能识别（迁移自老系统 B/C/E 插件）与多次取数标记。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="查一下近7天的GMV趋势，画个图")],
            "session_id": "t7",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "metric_query"
    assert "chart_visualization" in result["skills"]
    assert "图表可视化" in result["final_answer"]


async def test_metric_name_fallback_to_query():
    """LLM/mock 判为闲聊但 query 命中指标名 -> 修正为取数（老 find_max_match 兜底）。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="GMV")],
            "session_id": "t8",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "metric_query"
    assert result["query_elements"]["metrics"] == ["GMV"]


def test_apply_fixups_rules():
    """规则修正单元测试（迁移老系统 find_max_match / check_result 兜底语义）。"""
    from dmf_agent.agents.metric_agent.nodes.classify_intent import _apply_fixups
    from dmf_agent.schemas.agent import IntentResult

    # query 与候选指标完全一致 -> 转取数（老 find_max_match）
    fixed = _apply_fixups(IntentResult(intent="chitchat"), "GMV", ["GMV"])
    assert fixed.intent == "metric_query"

    # 金融领域关键词兜底 -> 转取数（老 check_result）
    fixed = _apply_fixups(IntentResult(intent="chitchat"), "对公存款", [])
    assert fixed.intent == "metric_query"

    # 普通闲聊不受影响
    kept = _apply_fixups(IntentResult(intent="chitchat"), "你好", ["GMV"])
    assert kept.intent == "chitchat"


async def test_help_intent_direct_reply():
    """help 控制意图：不取数，直接给能力说明。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="你能做什么？怎么用？")], "session_id": "h1", "user_id": "t"}
    )
    assert result["intent"] == "help"
    assert "query_elements" not in result
    assert "GMV" in result["final_answer"]


async def test_exit_intent_direct_reply():
    """exit 控制意图：不取数，告别回复。"""
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="再见，退出登录")], "session_id": "e1", "user_id": "t"}
    )
    assert result["intent"] == "exit"
    assert "query_elements" not in result
    assert result["final_answer"]


async def test_help_word_with_metric_still_queries():
    """含帮助词但明确取数（出现指标名）时不被 help 截胡。"""
    from dmf_agent.agents.metric_agent.nodes.classify_intent import _rule_based_intent

    result = _rule_based_intent("帮我查下近7天GMV", ["GMV"])
    assert result.intent == "metric_query"
