"""Phase 4d 报告生成测试：描述性统计 + 报告上下文（mock 模式）。"""

from typing import Any

from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.analysis.stats import describe_data

QR = {
    "columns": ["统计时间", "GMV"],
    "rows": [
        ["2026-08-01", 100],
        ["2026-08-02", 200],
        ["2026-08-03", 300],
    ],
}


# ---- describe_data（描述性统计 + 变异系数） ----


def test_describe_data_numeric():
    desc = describe_data(QR)
    assert set(desc["GMV"].keys()) == {"count", "mean", "std", "min", "max", "cv"}
    assert desc["GMV"]["count"] == 3
    assert desc["GMV"]["mean"] == 200
    assert desc["GMV"]["min"] == 100
    assert desc["GMV"]["max"] == 300


def test_describe_data_skips_non_numeric():
    # 时间列（字符串）不进统计
    assert "统计时间" not in describe_data(QR)


def test_describe_data_empty():
    assert describe_data({"columns": [], "rows": []}) == {}


# ---- _build_context（报告输入） ----


def _state(**overrides):
    base = {
        "query_elements": {"metrics": ["GMV"], "dimensions": [], "time_range": None},
        "dsl": {"metrics": [{"code": "gmv", "name": "GMV"}], "dimensions": []},
        "query_result": QR,
        "analysis": {"summary": "GMV 呈上升趋势", "stats": {}},
        "intent": "metric_query",
    }
    base.update(overrides)
    return base


def test_build_context_includes_data_and_stats():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _build_context

    ctx = _build_context(_state())
    assert "数据明细" in ctx
    assert "描述性统计" in ctx
    assert "GMV" in ctx
    assert "归因分析" not in ctx


def test_build_context_includes_diagnosis():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _build_context

    state = _state(
        intent="diagnosis",
        diagnosis={
            "ok": True,
            "metric": "GMV",
            "change": {
                "begin_time": "2026-08-01",
                "end_time": "2026-08-07",
                "metrics_value": 150,
                "compare_metrics_value": 160,
                "change_value": 10,
                "change_rate": 0.0667,
            },
            "dimensions": {
                "门店": [
                    {
                        "dim_name": "门店",
                        "dim_value": "A",
                        "change_value": 20,
                        "effect_index": 2.0,
                        "effect_rate": 0.6667,
                    }
                ]
            },
            "primary_dim": "门店",
            "global_summary": "GMV 从150变为160，上升了10",
            "smart_summary": "主要得益于门店是A的上升",
            "same_contribution_list": [{"dim_name": "门店", "dim_value": "A", "change_value": 20}],
            "opposite_contribution_list": [],
            "action_hints": ["优先排查反向贡献 TOP：门店=B（影响值 -10）"],
        },
    )
    ctx = _build_context(state)
    assert "归因分析" in ctx
    assert "GMV" in ctx
    assert "门店" in ctx
    assert "数据明细" in ctx


def test_build_context_diagnosis_requires_inference_label():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _build_context

    state = _state(
        intent="diagnosis",
        diagnosis={
            "ok": True,
            "metric": "GMV",
            "change": {
                "begin_time": "2026-08-01",
                "end_time": "2026-08-07",
                "metrics_value": 150,
                "compare_metrics_value": 160,
                "change_value": 10,
                "change_rate": 0.0667,
            },
            "dimensions": {
                "门店": [
                    {"dim_name": "门店", "dim_value": "A", "change_value": 20, "effect_index": 2.0, "effect_rate": 0.6667}
                ]
            },
            "primary_dim": "门店",
            "global_summary": "GMV 从150变为160，上升了10",
            "smart_summary": "主要得益于门店是A的上升",
            "same_contribution_list": [{"dim_name": "门店", "dim_value": "A", "change_value": 20}],
            "opposite_contribution_list": [],
            "action_hints": ["优先排查反向贡献 TOP：门店=B（影响值 -10）"],
        },
    )
    ctx = _build_context(state)
    # FR3：LLM 上下文必须要求标注"基于当前数据推断"，并注入完整确定性归因数据
    assert "基于当前数据推断" in ctx
    assert "行动建议线索" in ctx
    assert "影响占比" in ctx


def test_ensure_inference_label():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _ensure_inference_label

    # 无标注 -> 兜底补上
    assert _ensure_inference_label("GMV 上升。").endswith("（基于当前数据推断）")
    # 已标注 -> 不重复追加
    labeled = "GMV 上升。\n\n（基于当前数据推断）"
    assert _ensure_inference_label(labeled) == labeled
    # 空文本不报错
    assert _ensure_inference_label("") == ""


def test_build_context_no_data_no_table():
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _build_context

    ctx = _build_context(_state(query_result={}))
    assert "数据明细" not in ctx


# ---- report.yaml 报告结构 ----


def test_report_prompt_has_structure():
    system = load_prompt("report")["system"]
    assert "归因分析报告" in system
    assert "数据洞察报告" in system
    assert "指标变化概况" in system
    assert "重要发现" in system
    assert "禁止编造数字" in system


# ---- LLM 空内容兜底（避免产出空答复） ----


async def test_synthesize_empty_llm_content_falls_back_to_template():
    """LLM 返回空/纯空白内容时，必须回退确定性模板而非产出空答复。"""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatResult

    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import (
        build_synthesize_answer_node,
    )
    from dmf_agent.config.settings import Settings
    from dmf_agent.semantic.layer import SemanticLayer

    class _EmptyLLM(BaseChatModel):
        async def ainvoke(self, *a: Any, **k: Any) -> AIMessage:
            return AIMessage(content="   ")

        def _generate(self, *a: Any, **k: Any) -> Any:
            return ChatResult(generations=[])

        @property
        def _llm_type(self) -> str:
            return "fake-empty"

    deps = AgentDeps(
        settings=Settings(),
        get_chat_model=lambda: _EmptyLLM(),
        semantic_layer=SemanticLayer(),
    )
    node = build_synthesize_answer_node(deps)
    out = await node(_state())
    assert out["final_answer"].strip(), "空 LLM 输出不得产出空答复"
    assert out.get("mock_mode") is True
