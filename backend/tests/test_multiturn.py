"""Phase 6 多轮确认测试（无 LLM，取数用假语义引擎）。

单轮：validate_elements 失败 -> ask_clarification 输出问题 + 挂起确认（confirmation）；
多轮：ChatService 检测上轮挂起确认 -> 组合"原始问题 + 答复"注入 confirm_combined ->
extract_elements 用组合文本重新抽取（等价老 isUserConfirmation 状态还原）。
"""

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
from fakes import make_deps

# 无任何指标命中（且非闲聊/知识类）的问题 -> 触发追问分支
CLARIFY_QUERY = "统计一下情况怎么样"


def _deps() -> AgentDeps:
    return make_deps()


async def test_clarify_returns_confirmation():
    graph = build_metric_agent_graph(_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content=CLARIFY_QUERY)],
            "session_id": "m1",
            "user_id": "tester",
        }
    )
    # 无指标 -> 追问分支，且带挂起确认（problems + original_query）
    assert result["validation"]["ok"] is False
    assert result["confirmation"]["problems"] == ["未能从问题中识别出任何业务指标"]
    assert result["confirmation"]["original_query"] == CLARIFY_QUERY
    assert "补充" in result["final_answer"]


async def test_extract_uses_confirm_combined():
    from dmf_agent.agents.metric_agent.nodes.extract_elements import (
        build_extract_elements_node,
    )

    deps = _deps()
    node = build_extract_elements_node(deps)
    result = await node(
        {
            "messages": [HumanMessage(content="GMV")],
            # 多轮确认组合文本：原始问题 + 答复
            "confirm_combined": f"{CLARIFY_QUERY} GMV",
            "session_id": "m2",
            "user_id": "tester",
        }
    )
    assert result["query_elements"]["metrics"] == ["GMV"]


# ---- API 集成：多轮确认端到端 ----
# 注意：此处走真实应用装配（未配置 StarRocks -> execute_query 以 error 结果返回），
# 验证的是"追问 -> 组合重抽 -> 命中指标 -> 走通取数链路"的编排，而非数据本身。


def test_api_multiturn_confirmation():
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import app

    with TestClient(app) as client:
        # 第一轮：缺指标 -> 追问
        r1 = client.post(
            "/api/v1/chat", json={"message": CLARIFY_QUERY, "user_id": "u_mt"}
        )
        assert r1.status_code == 200
        d1 = r1.json()
        session_id = d1["session_id"]
        assert "补充" in d1["content"]

        # 上一条助手消息带挂起确认
        messages = client.get(f"/api/v1/sessions/{session_id}/messages").json()
        assert messages[-1]["node_trace"].get("confirmation", {}).get("problems")

        # 第二轮：答复指标名 -> 组合重新抽取 -> 命中 GMV 走通全链路
        r2 = client.post(
            "/api/v1/chat",
            json={"message": "GMV", "user_id": "u_mt", "session_id": session_id},
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["intent"] == "metric_query"
        assert "GMV" in d2["content"]

        # 挂起确认已被消费（该轮不再有 confirmation trace）
        messages2 = client.get(f"/api/v1/sessions/{session_id}/messages").json()
        assert "confirmation" not in messages2[-1]["node_trace"]


def test_api_normal_query_no_confirmation():
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/chat", json={"message": "查一下近7天的GMV趋势", "user_id": "u_mt2"}
        )
        data = r.json()
        assert data["intent"] == "metric_query"
        messages = client.get(f"/api/v1/sessions/{data['session_id']}/messages").json()
        assert "confirmation" not in messages[-1]["node_trace"]
