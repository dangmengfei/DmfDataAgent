"""FR8 新建指标意图（create）测试：意图识别 + create_metric 节点落库 round-trip。

测试用 InMemorySemanticRegistryRepository 作为 deps.semantic_registry_repo，
验证 LLM 提案 -> 校验 -> 草稿落库 的端到端链路（无需真实 LLM / StarRocks）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.nodes.classify_intent import _rule_based_intent
from dmf_agent.agents.metric_agent.nodes.create_metric import (
    _layer_refs,
    _proposal_to_defn,
)
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.schemas.agent import MetricDraftProposal
from dmf_agent.semantic.layer import SemanticLayer


# ---- 让 astructured_invoke 走 with_structured_output 通道的假 LLM ----


class _Returner:
    def __init__(self, obj: Any) -> None:
        self.obj = obj

    async def ainvoke(self, *a: Any, **k: Any) -> Any:  # noqa: D401
        return self.obj


class _FakeStructuredLLM(BaseChatModel):
    proposal: dict

    def with_structured_output(self, schema: Any, **kw: Any) -> _Returner:  # type: ignore[override]
        return _Returner(self.proposal)

    async def ainvoke(self, *a: Any, **k: Any) -> AIMessage:  # noqa: D401
        return AIMessage(content="")

    def _generate(self, *a: Any, **k: Any) -> Any:  # noqa: D401
        from langchain_core.outputs import ChatResult

        return ChatResult(generations=[])

    @property
    def _llm_type(self) -> str:  # type: ignore[override]
        return "fake"


def _deps_with_registry(layer: SemanticLayer, proposal: dict) -> AgentDeps:
    from dmf_agent.config.settings import Settings

    repo = InMemorySemanticRegistryRepository()
    deps = AgentDeps(
        settings=Settings(),
        get_chat_model=lambda: _FakeStructuredLLM(proposal=proposal),
        semantic_layer=layer,
        semantic_registry_repo=repo,
    )
    return deps


def test_rule_based_create_intent():
    assert _rule_based_intent("帮我创建一个指标", []).intent == "create"
    assert _rule_based_intent("新建一个指标叫月活客户数", []).intent == "create"
    # 查询已有指标不应误判为 create
    assert _rule_based_intent("查一下新建客户数", []).intent == "metric_query"
    assert _rule_based_intent("GMV 同比", []).intent == "metric_query"


def test_proposal_to_defn_simple():
    p = MetricDraftProposal(
        name="月活客户数", metric_type="simple", measure="cust_cnt", description="去重客户数"
    )
    d = _proposal_to_defn(p)
    assert d["metric_type"] == "simple"
    assert d["type_params"]["measure"] == "cust_cnt"
    assert d["name"] == "月活客户数"


def test_proposal_to_defn_ratio():
    p = MetricDraftProposal(
        name="存贷比", metric_type="ratio", numerator="loan_bal", denominator="deposit_bal"
    )
    d = _proposal_to_defn(p)
    assert d["type_params"]["numerator"] == "loan_bal"
    assert d["type_params"]["denominator"] == "deposit_bal"


def test_layer_refs_collects_measures_and_metrics():
    layer = SemanticLayer()
    measure_keys, all_metric_defs = _layer_refs(layer)
    assert measure_keys  # 默认 manifest 含 measures
    assert all_metric_defs  # 默认 manifest 含 metrics


async def test_create_metric_draft_roundtrip():
    from dmf_agent.agents.metric_agent.nodes.create_metric import build_create_metric_node

    layer = SemanticLayer()
    # 取一个真实存在的 measure 作为 simple 指标依赖
    measure = next(iter(_layer_refs(layer)[0]))
    proposal = {
        "name": "测试新建指标A",
        "description": "单测构造",
        "metric_type": "simple",
        "measure": measure,
    }
    deps = _deps_with_registry(layer, proposal)
    node = build_create_metric_node(deps)
    state = {
        "session_id": "s1",
        "user_id": "u1",
        "messages": [],
        "query_rewritten": "创建一个指标叫测试新建指标A",
    }
    out = await node(state)
    cr = out["create_result"]
    assert cr["ok"] is True
    assert cr["status"] == "draft"
    assert cr["name"] == "测试新建指标A"
    # 草稿确实落库
    item = await deps.semantic_registry_repo.get_metric(cr["metric_key"])
    assert item is not None and item["status"] == "draft"


async def test_create_metric_schema_invalid_no_persist():
    from dmf_agent.agents.metric_agent.nodes.create_metric import build_create_metric_node

    layer = SemanticLayer()
    # simple 指标必须有 measure，这里留空 -> schema 闸拦截，不落库
    proposal = {
        "name": "缺度量指标",
        "metric_type": "simple",
        "measure": None,
    }
    deps = _deps_with_registry(layer, proposal)
    node = build_create_metric_node(deps)
    state = {
        "session_id": "s2",
        "user_id": "u2",
        "messages": [],
        "query_rewritten": "创建一个指标",
    }
    out = await node(state)
    cr = out["create_result"]
    assert cr["ok"] is False
    assert any("measure" in p for p in (cr.get("problems") or []))
    # 未落库
    listed, _ = await deps.semantic_registry_repo.list_metrics(keyword="缺度量指标")
    assert listed == []


async def test_create_graph_flow_routes_to_create_metric():
    """图级端到端：create 意图 -> create_metric 节点 -> 草稿落库 -> 答复渲染。

    挂在 test_graph 之外的独立文件里，用 InMemory 注册中心 + 假结构化 LLM，
    验证 _route_by_intent 的 create 分支与 create_metric -> synthesize 的连线确实生效。
    """
    from fakes import make_deps

    from dmf_agent.agents.metric_agent import build_metric_agent_graph

    layer = SemanticLayer()
    measure = next(iter(_layer_refs(layer)[0]))
    proposal = {
        "name": "图级新建指标B",
        "description": "图级单测构造",
        "metric_type": "simple",
        "measure": measure,
    }
    repo = InMemorySemanticRegistryRepository()
    deps = make_deps(
        layer=layer,
        get_chat_model=lambda: _FakeStructuredLLM(proposal=proposal),
        semantic_registry_repo=repo,
    )
    graph = build_metric_agent_graph(deps)
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="帮我创建一个指标叫图级新建指标B")],
            "session_id": "g1",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "create"
    cr = result.get("create_result") or {}
    assert cr.get("ok") is True and cr.get("status") == "draft"
    # 答复渲染需体现草稿待审批
    assert "审批" in result["final_answer"] or "草稿" in result["final_answer"]
    # 未误走取数链路
    assert result.get("dsl") is None


async def test_create_metric_no_llm_returns_guidance():
    from dmf_agent.agents.metric_agent.nodes.create_metric import build_create_metric_node

    layer = SemanticLayer()
    deps = AgentDeps(
        settings=__import__("dmf_agent.config.settings", fromlist=["Settings"]).Settings(),
        get_chat_model=lambda: None,  # 无 LLM
        semantic_layer=layer,
        semantic_registry_repo=InMemorySemanticRegistryRepository(),
    )
    node = build_create_metric_node(deps)
    state = {
        "session_id": "s3",
        "user_id": "u3",
        "messages": [],
        "query_rewritten": "创建一个指标",
    }
    out = await node(state)
    cr = out["create_result"]
    assert cr["ok"] is False
    assert cr.get("needs_llm") is True
