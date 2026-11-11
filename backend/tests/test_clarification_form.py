"""WorkBuddy 式结构化澄清确认测试。

覆盖三段：
1. `clarification.build_clarification`：载荷结构、候选**按置信度排名**、预选回填；
2. `apply_clarification.merge_selection`：提交选择的确定性合并（含未收录指标分流、时间三态）；
3. 图级端到端：校验不通过 -> 产出确认表单 -> 用户提交 -> **自动继续执行后续步骤**出结果。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent import build_metric_agent_graph
from dmf_agent.agents.metric_agent.clarification import (
    build_clarification,
    match_time_preset,
    resolve_time_preset,
)
from dmf_agent.agents.metric_agent.nodes.apply_clarification import merge_selection
from dmf_agent.semantic.layer import SemanticLayer
from fakes import make_deps


def _state(text: str, elements: Dict[str, Any], problems: list[str] | None = None) -> Dict[str, Any]:
    return {
        "messages": [HumanMessage(content=text)],
        "session_id": "s-form",
        "user_id": "tester",
        "query_elements": elements,
        "validation": {"ok": False, "problems": problems or [], "missed_metrics": []},
    }


# ---------------- 1. 载荷结构 / 候选置信度排名 ----------------


def test_build_clarification_structure_and_ranking():
    """候选按置信度降序：字面完全命中的 GMV 必须排第一，并带 score/source。"""
    deps = make_deps()
    state = _state(
        "查一下近7天各门店的GMV",
        {"metrics": ["GMV"], "dimensions": [], "time_range": resolve_time_preset("last_7_days")},
        problems=["未能从问题中识别出任何业务指标"],
    )
    payload = build_clarification(deps, state, state["validation"]["problems"])

    assert payload["type"] == "clarify"
    assert payload["rank_by"] == "confidence"
    assert [f["key"] for f in payload["fields"]] == ["metrics", "dimensions", "time_range"]

    metrics_field = payload["fields"][0]
    first = metrics_field["options"][0]
    assert first["value"] == "GMV"
    assert first["score"] == 1.0
    assert first["source"] in {"full", "black_talk"}
    assert "置信度" in first["desc"]
    assert metrics_field["selected"] == ["GMV"]
    assert metrics_field["allow_custom"] is True

    # 时间预设回填：已识别区间命中「近 7 天」
    time_field = payload["fields"][2]
    assert time_field["selected"] == ["last_7_days"]
    assert {o["value"] for o in time_field["options"]} >= {"last_7_days", "last_month"}

    # 提交时回传上下文（保留已识别要素与意图）
    assert payload["context"]["original_query"] is not None
    assert payload["context"]["partial_elements"]["metrics"] == ["GMV"]


def test_options_sorted_by_confidence_descending():
    """所有带分候选必须满足 score 单调不增（无分候选置底）。"""
    deps = make_deps()
    state = _state(
        "各渠道的GMV和订单数",
        {"metrics": [], "dimensions": ["渠道"], "time_range": {}},
        problems=["未能从问题中识别出任何业务指标"],
    )
    payload = build_clarification(deps, state, state["validation"]["problems"])
    for field in payload["fields"]:
        scores = [o.get("score") for o in field["options"]]
        scored = [s for s in scores if isinstance(s, (int, float))]
        assert scored == sorted(scored, reverse=True), f"{field['key']} 未按置信度排序"
        # 无分项（score=None，如"已识别"兜底）只能在末尾
        first_none = next((i for i, s in enumerate(scores) if s is None), len(scores))
        assert all(s is None for s in scores[first_none:])


def test_unrecognized_metric_preselected_and_flagged():
    """用户提到但未收录的指标：作为预选值回填、置底、并标注可发起创建。"""
    deps = make_deps()
    state = _state(
        "查一下月活客户数",
        {"metrics": [], "unrecognized_metrics": ["月活客户数"], "dimensions": [], "time_range": {}},
        problems=["指标[月活客户数]未收录在语义层"],
    )
    state["validation"]["missed_metrics"] = ["月活客户数"]
    payload = build_clarification(deps, state, state["validation"]["problems"])

    metrics_field = payload["fields"][0]
    assert metrics_field["selected"] == ["月活客户数"]
    picked = [o for o in metrics_field["options"] if o["value"] == "月活客户数"]
    assert picked and picked[0]["source"] == "selected"
    assert picked[0]["score"] is None  # 无置信度 -> 置底
    assert "创建" in picked[0]["desc"]
    assert metrics_field["options"][-1]["value"] == "月活客户数"


def test_dimension_options_limited_to_selected_metric_support():
    """维度候选限定为已选指标支持的维度（避免用户选出必然失败的组合）。"""
    deps = make_deps()
    layer: SemanticLayer = deps.semantic_layer
    metric = layer.resolve_metric("日活跃用户数")
    supported = set(layer.supported_dimensions(metric))
    assert supported, "用例前提：该指标有可用维度"

    state = _state(
        "日活跃用户数按各维度拆分",
        {"metrics": ["日活跃用户数"], "dimensions": [], "time_range": {}},
        problems=[],
    )
    payload = build_clarification(deps, state, [])
    dim_values = {o["value"] for o in payload["fields"][1]["options"]}
    assert dim_values <= supported
    assert layer.time_dimension_name() not in dim_values


def test_unsupported_preselected_dimension_is_dropped_and_flagged():
    """已识别但该指标不支持的维度：不入候选、不预选，单独下发 invalid 供前端红字提示。

    回归用例：曾把不支持的维度留在候选中，用户"改选"后变成两个维度一起提交，
    仍被拒 -> 反复澄清。
    """
    deps = make_deps()
    state = _state(
        "日活跃用户数按地区拆分",
        {"metrics": ["日活跃用户数"], "dimensions": ["地区"], "time_range": {}},
        problems=["维度[地区]不被指标（日活跃用户数）所在数据域支持（该指标可用维度：统计日期/渠道/平台）"],
    )
    payload = build_clarification(deps, state, state["validation"]["problems"])
    dim_field = payload["fields"][1]

    assert dim_field["invalid"] == ["地区"]
    assert dim_field["selected"] == []  # 不支持 -> 不预选，避免"原样再提交"死循环
    values = {o["value"] for o in dim_field["options"]}
    assert "地区" not in values
    assert values <= {"渠道", "平台"}
    assert "仅可选" in dim_field["hint"]


async def test_graph_resubmit_after_dimension_fix_succeeds():
    """按表单给出的候选改选维度后，提交即通过校验并出数（澄清闭环）。"""
    graph = build_metric_agent_graph(make_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="已确认：指标=日活跃用户数；维度=渠道；时间=近7天")],
            "session_id": "t-form3",
            "user_id": "tester",
            "clarify_selection": {
                "metrics": ["日活跃用户数"],
                "dimensions": ["渠道"],
                "time_range": "last_7_days",
                "context": {
                    "intent": "metric_query",
                    "original_query": "日活跃用户数按地区拆分",
                    "partial_elements": {"metrics": ["日活跃用户数"], "dimensions": ["地区"]},
                },
            },
        }
    )
    assert result["validation"]["ok"] is True
    assert result["query_elements"]["dimensions"] == ["渠道"]
    assert result["query_result"]["row_count"] == 6  # 3 天 × 2 渠道
    assert "日活跃用户数" in result["final_answer"]


# ---------------- 2. 提交合并 ----------------


def test_merge_selection_keeps_base_and_normalizes():
    """合并规则：覆盖指标/维度/时间，保留排序/同环比，置信度拉满避免再次澄清。"""
    deps = make_deps()
    base = {
        "metrics": ["订单数"],
        "dimensions": ["渠道"],
        "time_range": {"start": "2026-01-01", "end": "2026-01-31", "granularity": "day"},
        "order_by": "GMV desc",
        "limit": 5,
        "period_comparison": True,
        "period_comparison_type": "mom",
    }
    selection = {
        "metrics": ["GMV"],
        "dimensions": ["门店"],
        "time_range": "last_7_days",
    }
    elements = merge_selection(base, selection, deps)

    assert elements["metrics"] == ["GMV"]
    assert elements["dimensions"] == ["门店"]
    assert elements["time_range"] == resolve_time_preset("last_7_days")
    # 用户没改的识别结果必须保留
    assert elements["order_by"] == "GMV desc"
    assert elements["limit"] == 5
    assert elements["period_comparison_type"] == "mom"
    assert elements["confidence"] == 1.0


def test_merge_selection_splits_unrecognized_metrics():
    """提交的指标名按语义层收录情况分流（未收录进 unrecognized_metrics，保持引导语义）。"""
    deps = make_deps()
    elements = merge_selection({}, {"metrics": ["GMV", "月活客户数"]}, deps)
    assert elements["metrics"] == ["GMV"]
    assert elements["unrecognized_metrics"] == ["月活客户数"]


def test_merge_selection_empty_dimensions_means_all():
    """显式提交空维度 = 查全量汇总（不应沿用旧维度）。"""
    deps = make_deps()
    elements = merge_selection({"dimensions": ["渠道"]}, {"metrics": ["GMV"], "dimensions": []}, deps)
    assert elements["dimensions"] == []


def test_merge_time_range_variants():
    """时间三态：预设 key / {start,end} / "起 ~ 止" 文本。"""
    deps = make_deps()
    assert merge_selection({}, {"time_range": "last_month"}, deps)["time_range"] == (
        resolve_time_preset("last_month")
    )
    assert merge_selection(
        {}, {"time_range": {"start": "2026-02-01", "end": "2026-02-28"}}, deps
    )["time_range"] == {"start": "2026-02-01", "end": "2026-02-28", "granularity": "day"}
    assert merge_selection({}, {"time_range": "2026-03-01 ~ 2026-03-31"}, deps)["time_range"] == {
        "start": "2026-03-01",
        "end": "2026-03-31",
        "granularity": "day",
    }
    # 无法解析时沿用原时间窗，不抛错
    base = {"time_range": {"start": "2026-01-01", "end": "2026-01-07", "granularity": "day"}}
    assert merge_selection(base, {"time_range": "随便什么时候"}, deps)["time_range"] == base["time_range"]


def test_time_preset_helpers_are_self_consistent():
    ref = date(2026, 9, 17)
    assert resolve_time_preset("last_7_days", ref)["start"] == "2026-09-11"
    assert resolve_time_preset("last_month", ref) == {
        "start": "2026-08-01",
        "end": "2026-08-31",
        "granularity": "day",
    }
    assert resolve_time_preset("unknown") is None
    assert match_time_preset(resolve_time_preset("this_month", ref), ref) == "this_month"


# ---------------- 3. 图级端到端：澄清 -> 提交 -> 继续执行 ----------------


async def test_graph_clarification_form_then_submit_continues():
    """未通过校验 -> 产出确认表单；提交选择后自动续跑取数链路并出结果。"""
    graph = build_metric_agent_graph(make_deps())

    # 第一轮：识别不出指标 -> 追问 + 结构化确认表单
    first = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="统计一下情况怎么样")],
            "session_id": "t-form",
            "user_id": "tester",
        }
    )
    assert first["validation"]["ok"] is False
    form = first["confirmation"]["clarification"]
    assert form["type"] == "clarify" and form["fields"]
    assert form["submit_label"] == "确认并继续"

    # 第二轮：用户点选确认后提交（前端把选择 + context 原样回传）
    metrics_options = form["fields"][0]["options"]
    assert any(o["value"] == "GMV" for o in metrics_options), "表单应能选到 GMV"

    second = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="已确认：指标=GMV；维度=门店；时间=近7天")],
            "session_id": "t-form",
            "user_id": "tester",
            "clarify_selection": {
                "metrics": ["GMV"],
                "dimensions": ["门店"],
                "time_range": "last_7_days",
                "context": form["context"],
            },
        }
    )
    # 跳过改写/意图/抽取，直接续跑取数链路
    assert second.get("clarification_resolved") is True
    assert second["intent"] == "metric_query"
    assert second["query_elements"]["metrics"] == ["GMV"]
    assert second["query_elements"]["dimensions"] == ["门店"]
    assert second["validation"]["ok"] is True
    assert second["dsl"]["metrics"][0]["name"] == "GMV"
    assert second["query_result"]["mock"] is False
    assert second["query_result"]["row_count"] == 6  # 3 天 × 2 门店
    assert "GMV" in second["final_answer"]


async def test_chatservice_clarify_submission_continues_without_resubmitting():
    """对话服务级：澄清表单 -> 提交 -> 继续执行，且不再重复追问；free-text 补充走常规链路。"""
    from dmf_agent.config.settings import ObservabilitySettings, Settings
    from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
    from dmf_agent.services.chat_service import ChatService

    settings = Settings(observability=ObservabilitySettings(enabled=False))
    service = ChatService(
        graph=build_metric_agent_graph(make_deps()),
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
    )

    first = await service.chat(session_id=None, user_id="u1", message="统计一下情况怎么样")
    form = first.get("clarification")
    assert form and form["fields"], "首轮必须返回结构化确认表单"
    assert form["rank_by"] == "confidence"

    # 提交确认（纯选择）-> 自动续跑，无需用户重述
    second = await service.chat(
        session_id=first["session_id"],
        user_id="u1",
        message="已确认：指标=GMV；维度=门店；时间=近7天",
        clarify_selection={
            "metrics": ["GMV"],
            "dimensions": ["门店"],
            "time_range": "last_7_days",
            "context": form["context"],
        },
    )
    trace = second.get("node_trace") or {}
    assert second["intent"] == "metric_query"
    assert (trace.get("query_result") or {}).get("row_count") == 6
    assert trace.get("clarification") in (None, {}), "已提交后不应再挂起同一澄清"
    assert not trace.get("query_rewrite"), "确定性续跑不应再走改写/抽取链路"

    # 带"补充说明"自由文本 -> 改走常规链路（LLM 抽取），补充语义不被吞掉
    third = await service.chat(
        session_id=first["session_id"],
        user_id="u1",
        message="已确认：指标=GMV；维度=门店；时间=近7天；补充说明：只看A店",
        clarify_selection={
            "metrics": ["GMV"],
            "dimensions": ["门店"],
            "time_range": "last_7_days",
            "extra": "只看A店",
            "context": form["context"],
        },
    )
    third_trace = third.get("node_trace") or {}
    assert third_trace.get("query_rewrite"), "含补充说明时应走常规改写/抽取链路"


async def test_graph_submitted_unrecognized_metric_guides_creation():
    """提交未收录指标 -> 仍然只引导创建，不取数、不落草稿。"""
    graph = build_metric_agent_graph(make_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="已确认：指标=月活客户数")],
            "session_id": "t-form2",
            "user_id": "tester",
            "clarify_selection": {
                "metrics": ["月活客户数"],
                "dimensions": [],
                "context": {"intent": "metric_query", "original_query": "查一下月活客户数"},
            },
        }
    )
    assert result["validation"]["ok"] is False
    assert result["validation"]["missed_metrics"] == ["月活客户数"]
    assert "创建一个指标" in result["final_answer"]
    assert "不会自动创建指标" in result["final_answer"]
    # 未续跑取数链路
    assert "query_result" not in result
