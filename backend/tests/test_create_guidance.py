"""FR8 未命中引导创建测试：未收录指标 -> 引导用户显式创建，**不自动创建**。

覆盖三级判定（精准 / 通用 / 不引导）、validate_elements 的 missed_metrics 产出、
ask_clarification 的引导渲染，以及"未命中路径绝不落草稿"的硬约束。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from dmf_agent.agents.metric_agent.guidance import (
    build_create_guidance,
    build_generic_create_guidance,
    build_llm_guidance_instruction,
    resolve_create_guidance,
)
from dmf_agent.agents.metric_agent.nodes.ask_clarification import (
    build_ask_clarification_node,
)
from dmf_agent.agents.metric_agent.nodes.validate_elements import (
    build_validate_elements_node,
)
from dmf_agent.db.memory import InMemorySemanticRegistryRepository
from dmf_agent.semantic.layer import SemanticLayer
from fakes import make_deps

_CREATE_ENTRY = "创建一个指标"
_NO_AUTO_NOTE = "我不会自动创建指标"


class _ReturnerStub:
    """假装 `with_structured_output` 的返回：直接给出预置对象。"""

    def __init__(self, obj: Any) -> None:
        self.obj = obj

    async def ainvoke(self, *a: Any, **k: Any) -> Any:  # noqa: D401
        return self.obj


# ---- guidance 单元（三级判定） ----


def test_guidance_precise_when_missed_metrics():
    text = build_create_guidance(["月活客户数"])
    assert "月活客户数" in text
    assert _CREATE_ENTRY in text
    assert _NO_AUTO_NOTE in text
    # 精准引导要给出可照抄的示例
    assert "名叫月活客户数" in text


def test_guidance_generic_has_no_metric_name():
    text = build_generic_create_guidance()
    assert _CREATE_ENTRY in text
    assert _NO_AUTO_NOTE in text


def test_resolve_guidance_precise_takes_priority():
    v = {"problems": ["指标[X]未收录在语义层"], "missed_metrics": ["X"]}
    assert "X" in resolve_create_guidance(v)


def test_resolve_guidance_generic_when_no_metric_at_all():
    v = {"problems": ["未能从问题中识别出任何业务指标"], "missed_metrics": []}
    text = resolve_create_guidance(v)
    assert _CREATE_ENTRY in text


def test_resolve_guidance_silent_for_low_confidence_and_unknown_dimension():
    """指标本身存在（低置信澄清 / 维度未收录）时**不得**引导建指标，避免误导。"""
    assert resolve_create_guidance({"problems": ["识别置信度较低（0.30），请确认"], "missed_metrics": []}) == ""
    assert resolve_create_guidance({"problems": ["维度[地区]未收录在语义层"], "missed_metrics": []}) == ""
    assert resolve_create_guidance(None) == ""
    assert resolve_create_guidance({"problems": [], "missed_metrics": []}) == ""


def test_llm_guidance_instruction_forbids_auto_create():
    inst = build_llm_guidance_instruction(["月活客户数"])
    assert "月活客户数" in inst
    assert "严禁声称已自动创建" in inst
    assert build_llm_guidance_instruction([]) == ""


# ---- validate_elements：missed_metrics 产出 ----


async def test_validate_elements_collects_missed_metrics():
    """用户提到的指标名不在语义层 -> 进 missed_metrics；确有的指标不进。"""
    layer = SemanticLayer()
    real_metric = layer.list_metrics()[0].name
    deps = make_deps(layer=layer)
    node = build_validate_elements_node(deps)

    out = await node({"query_elements": {"metrics": [real_metric, "不存在的指标ZZ"], "dimensions": []}})
    v = out["validation"]
    assert v["missed_metrics"] == ["不存在的指标ZZ"]
    assert v["ok"] is False


async def test_validate_elements_no_missed_when_all_resolved():
    layer = SemanticLayer()
    real_metric = layer.list_metrics()[0].name
    deps = make_deps(layer=layer)
    node = build_validate_elements_node(deps)

    out = await node({"query_elements": {"metrics": [real_metric], "dimensions": []}})
    assert out["validation"]["missed_metrics"] == []
    assert out["validation"]["ok"] is True


async def test_validate_elements_no_missed_when_nothing_recognized():
    """完全没识别出指标 -> missed 为空（走通用引导，而非点名）。"""
    deps = make_deps()
    node = build_validate_elements_node(deps)
    out = await node({"query_elements": {"metrics": [], "dimensions": []}})
    v = out["validation"]
    assert v["missed_metrics"] == []
    assert v["ok"] is False


# ---- ask_clarification：引导渲染 + 不自动创建 ----


async def test_clarification_appends_precise_guidance_and_creates_no_draft():
    deps = make_deps(semantic_registry_repo=InMemorySemanticRegistryRepository())
    node = build_ask_clarification_node(deps)
    out = await node(
        {
            "session_id": "cg1",
            "validation": {
                "ok": False,
                "problems": ["指标[月活客户数]未收录在语义层"],
                "missed_metrics": ["月活客户数"],
            },
            "messages": [],
        }
    )
    answer = out["final_answer"]
    assert _CREATE_ENTRY in answer
    assert "月活客户数" in answer
    assert _NO_AUTO_NOTE in answer
    # 追问挂起信息照常返回，供多轮确认
    assert out["confirmation"]["problems"]
    # 硬约束：只引导，不落草稿
    listed, _ = await deps.semantic_registry_repo.list_metrics()
    assert listed == []


async def test_clarification_appends_generic_guidance_when_no_metric():
    deps = make_deps()
    node = build_ask_clarification_node(deps)
    out = await node(
        {
            "session_id": "cg2",
            "validation": {"ok": False, "problems": ["未能从问题中识别出任何业务指标"]},
            "messages": [],
        }
    )
    assert _CREATE_ENTRY in out["final_answer"]


async def test_clarification_no_guidance_for_low_confidence():
    """低置信澄清（指标存在）不应引导建指标。"""
    deps = make_deps()
    node = build_ask_clarification_node(deps)
    out = await node(
        {
            "session_id": "cg3",
            "validation": {
                "ok": False,
                "problems": ["问题中指标/口径的识别置信度较低（0.30），请确认要查询的具体指标与口径"],
                "missed_metrics": [],
            },
            "messages": [],
        }
    )
    assert _CREATE_ENTRY not in out["final_answer"]


async def test_clarification_dedupes_llm_guidance():
    """LLM 已给出创建入口时，不重复追加确定性引导块。"""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatResult

    class _Llm(BaseChatModel):
        async def ainvoke(self, *a: Any, **k: Any) -> AIMessage:
            return AIMessage(
                content="你说的『月活客户数』尚未收录。如需新增，请直接说：创建一个指标，名叫月活客户数。"
            )

        def _generate(self, *a: Any, **k: Any) -> Any:
            return ChatResult(generations=[])

        @property
        def _llm_type(self) -> str:
            return "fake-clarify"

    deps = make_deps(get_chat_model=lambda: _Llm())
    node = build_ask_clarification_node(deps)
    out = await node(
        {
            "session_id": "cg4",
            "validation": {
                "ok": False,
                "problems": ["指标[月活客户数]未收录在语义层"],
                "missed_metrics": ["月活客户数"],
            },
            "messages": [],
        }
    )
    # 只出现一次创建入口，且不留确定性兜底块的边界声明
    assert out["final_answer"].count(_CREATE_ENTRY) == 1
    assert _NO_AUTO_NOTE not in out["final_answer"]


# ---- unrecognized_metrics 通道（LLM 报告未收录指标名的主渠道） ----


async def test_validate_elements_reads_unrecognized_metrics():
    """LLM 把未收录指标放进 unrecognized_metrics -> 进 missed_metrics，且文案改为"未收录"。"""
    layer = SemanticLayer()
    deps = make_deps(layer=layer)
    node = build_validate_elements_node(deps)
    out = await node(
        {
            "query_elements": {
                "metrics": [],
                "unrecognized_metrics": ["月活客户数"],
                "dimensions": [],
            }
        }
    )
    v = out["validation"]
    assert v["missed_metrics"] == ["月活客户数"]
    assert v["ok"] is False
    joined = "；".join(v["problems"])
    assert "未收录在语义层" in joined
    # 用户确实提到了指标名，不该报"没识别出任何指标"
    assert "未能从问题中识别出任何业务指标" not in joined


async def test_validate_elements_dedupes_both_sources():
    """两个来源同名时只报一次（unrecognized_metrics 与 metrics 兜底）。"""
    layer = SemanticLayer()
    deps = make_deps(layer=layer)
    node = build_validate_elements_node(deps)
    out = await node(
        {
            "query_elements": {
                "metrics": ["月活客户数"],
                "unrecognized_metrics": ["月活客户数"],
                "dimensions": [],
            }
        }
    )
    assert out["validation"]["missed_metrics"] == ["月活客户数"]


class _SchemaAwareLLM(BaseChatModel):
    """按 schema 返回指定载荷的假 LLM（未登记 schema 返回空 dict -> 上层走兜底）。"""

    payloads: dict

    def with_structured_output(self, schema: Any, **kw: Any) -> _ReturnerStub:  # type: ignore[override]
        return _ReturnerStub(self.payloads.get(getattr(schema, "__name__", ""), {}))

    def _generate(self, *a: Any, **k: Any) -> Any:
        from langchain_core.outputs import ChatResult

        return ChatResult(generations=[])

    @property
    def _llm_type(self) -> str:  # type: ignore[override]
        return "fake-schema-aware"


async def test_extract_elements_llm_path_keeps_unrecognized_metrics():
    """LLM 抽取路径必须把 unrecognized_metrics 一路带到 query_elements。"""
    from dmf_agent.agents.metric_agent.nodes.extract_elements import (
        build_extract_elements_node,
    )
    from dmf_agent.schemas.agent import QueryElements

    payload = QueryElements(metrics=[], unrecognized_metrics=["月活客户数"], dimensions=[])
    deps = make_deps(
        get_chat_model=lambda: _SchemaAwareLLM(payloads={"QueryElements": payload})
    )
    node = build_extract_elements_node(deps)
    out = await node({"messages": [], "query_rewritten": "查一下近7天月活客户数的趋势"})
    elements = out["query_elements"]
    assert elements["unrecognized_metrics"] == ["月活客户数"]
    assert elements["metrics"] == []


async def test_miss_flow_guides_user_and_creates_no_draft():
    """端到端串联：抽取(未命中) -> 校验 -> 追问引导创建，全程不落草稿。"""
    from dmf_agent.agents.metric_agent.nodes.extract_elements import (
        build_extract_elements_node,
    )
    from dmf_agent.schemas.agent import QueryElements

    payload = QueryElements(metrics=[], unrecognized_metrics=["月活客户数"], dimensions=[])
    repo = InMemorySemanticRegistryRepository()
    deps = make_deps(
        get_chat_model=lambda: _SchemaAwareLLM(payloads={"QueryElements": payload}),
        semantic_registry_repo=repo,
    )
    state: dict = {"messages": [], "query_rewritten": "查一下近7天月活客户数的趋势"}

    state.update(await build_extract_elements_node(deps)(state))
    state.update(await build_validate_elements_node(deps)(state))
    state.update(await build_ask_clarification_node(deps)(state))

    answer = state["final_answer"]
    assert _CREATE_ENTRY in answer
    assert "月活客户数" in answer
    assert _NO_AUTO_NOTE in answer
    # 硬约束：引导 ≠ 代劳，不得落草稿
    listed, _ = await repo.list_metrics()
    assert listed == []


# ---- synthesize_answer：LLM 上下文注入引导指令 ----


def test_build_context_injects_missed_metrics_instruction():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _build_context

    ctx = _build_context(
        {
            "intent": "metric_query",
            "query_elements": {"metrics": ["月活客户数"]},
            "validation": {"ok": False, "problems": [], "missed_metrics": ["月活客户数"]},
        }
    )
    assert "未收录指标：月活客户数" in ctx
    assert "严禁声称已自动创建" in ctx

    # 无未命中时不注入该指令
    ctx2 = _build_context({"intent": "metric_query", "query_elements": {"metrics": ["GMV"]}})
    assert "严禁声称已自动创建" not in ctx2
