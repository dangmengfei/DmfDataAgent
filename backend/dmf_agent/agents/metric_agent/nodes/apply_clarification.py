"""澄清提交节点：把用户在确认表单上的选择**确定性合并**为查询要素并续跑。

WorkBuddy 式反问澄清的后半程：`ask_clarification` 产出结构化表单（指标 / 维度 /
时间范围候选，按置信度排名），用户点选提交后，ChatService 把选择放进
`state["clarify_selection"]`，图从本节点入口开始（跳过 rewrite / classify /
extract，避免再次误解），合并要素后直接进入
`validate_elements -> build_dsl -> execute_query -> analyze_data -> synthesize_answer`。

合并规则（全部确定性，无 LLM 调用）：
- 指标：取用户选择；能 resolve 的进 `metrics`，未收录的进 `unrecognized_metrics`
  （保持"未命中 -> 引导创建"语义）；
- 维度：取用户选择（可多选，空 = 全量汇总）；
- 时间：预设 key / `{start,end}` / `"YYYY-MM-DD ~ YYYY-MM-DD"` 三种形态归一；
- 其余已识别要素（排序 / limit / 同环比 / 阈值 / 过滤）由表单回传的 `context.partial_elements`
  保留，用户在表单上没改的部分不会丢；
- 置信度置 1.0（用户已明确确认），避免再次触发低置信澄清。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from dmf_agent.agents.metric_agent.clarification import resolve_time_preset
from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState

logger = logging.getLogger(__name__)

# 数据链路意图（合并后若无法从上下文取到意图，默认按指标查询处理）
_DATA_CHAIN_INTENTS = ("metric_query", "dimension_query", "diagnosis")
# 时间自定义输入的分隔符：`2026-01-01 ~ 2026-01-31` / `2026-01-01,2026-01-31` / `2026-01-01至2026-01-31`
_TIME_SPLIT = re.compile(r"\s*(?:~|～|至|,|，|-{1,2}>)\s*")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _as_list(value: Any) -> List[str]:
    """把表单字段值归一为去重保序的字符串列表（支持 str / list / None）。"""
    out: List[str] = []
    if value is None:
        return out
    items = value if isinstance(value, (list, tuple, set)) else [value]
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _split_metrics(names: List[str], layer: Any) -> tuple[List[str], List[str]]:
    """按语义层收录情况把指标名分为（已收录, 未收录），保持用户选择顺序。"""
    recognized: List[str] = []
    unrecognized: List[str] = []
    for name in names:
        metric = None
        if layer is not None:
            try:
                metric = layer.resolve_metric(name)
            except Exception as exc:  # noqa: BLE001 - 解析失败按未收录处理，不阻断
                logger.warning("澄清提交：指标[%s]解析失败：%s", name, exc)
                metric = None
        (recognized if metric is not None else unrecognized).append(name)
    return recognized, unrecognized


def _merge_time_range(base: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    """归一用户提交的时间选择：预设 key / {start,end} / "起 ~ 止" 文本。"""
    if not raw:
        return base
    if isinstance(raw, dict):
        start, end = str(raw.get("start") or ""), str(raw.get("end") or "")
        if start and end:
            return {
                "start": start,
                "end": end,
                "granularity": str(raw.get("granularity") or "day"),
            }
        return base
    text = str(raw).strip()
    if not text:
        return base
    preset = resolve_time_preset(text)
    if preset is not None:
        return preset
    dates = _DATE.findall(text)
    if len(dates) >= 2:
        return {"start": dates[0], "end": dates[1], "granularity": "day"}
    if len(dates) == 1:
        return {"start": dates[0], "end": dates[0], "granularity": "day"}
    logger.warning("澄清提交：无法解析的时间范围[%s]，沿用原时间窗", text)
    return base


def _infer_intent(context_intent: str, elements: Dict[str, Any]) -> str:
    """续跑意图：优先沿用澄清前的意图，否则按要素推断为指标查询。"""
    intent = str(context_intent or "").strip()
    if intent in _DATA_CHAIN_INTENTS:
        return intent
    if elements.get("metrics") or elements.get("unrecognized_metrics"):
        return "metric_query"
    return intent or "metric_query"


def merge_selection(
    base_elements: Dict[str, Any], selection: Dict[str, Any], deps: AgentDeps
) -> Dict[str, Any]:
    """把表单选择合并进已识别要素（纯函数，便于单测）。"""
    layer = deps.semantic_layer
    elements: Dict[str, Any] = dict(base_elements or {})

    # FR5 空数据时间窗重定向：adjusted 窗口由后端在上一轮确定性算好（empty_trace
    # .compute_time_redirect），此处直接采用，指标/维度/过滤沿用 partial 基线不动。
    # time_redirected 标记防循环：续跑后仍为空时 analyze_data 不再下发重定向卡片。
    if str(selection.get("time_redirect") or "") == "latest":
        adjusted = selection.get("time_range")
        if isinstance(adjusted, dict) and adjusted.get("start") and adjusted.get("end"):
            elements["time_range"] = {
                "start": str(adjusted["start"]),
                "end": str(adjusted["end"]),
                "granularity": str(adjusted.get("granularity") or "day"),
            }
            elements["time_redirected"] = True
            logger.info("时间窗重定向续跑：%s", elements["time_range"])
        elements["confidence"] = 1.0
        elements.setdefault("filters", [])
        return elements

    metrics = _as_list(selection.get("metrics"))
    if metrics:
        recognized, unrecognized = _split_metrics(metrics, layer)
        elements["metrics"] = recognized
        elements["unrecognized_metrics"] = unrecognized
    else:
        elements.setdefault("metrics", [])
        elements.setdefault("unrecognized_metrics", [])

    dimensions = _as_list(selection.get("dimensions"))
    if selection.get("dimensions") is not None:
        # 允许用户显式清空维度（改为查全量汇总）
        elements["dimensions"] = dimensions
    else:
        elements.setdefault("dimensions", [])

    elements["time_range"] = _merge_time_range(
        dict(elements.get("time_range") or {}), selection.get("time_range")
    )
    # 用户已明确确认 -> 置信度拉满，避免再次触发低置信澄清
    elements["confidence"] = 1.0
    elements.setdefault("filters", [])
    return elements


def build_apply_clarification_node(deps: AgentDeps):
    async def apply_clarification(state: AgentState) -> Dict[str, Any]:
        selection = dict(state.get("clarify_selection") or {})
        context = dict(selection.get("context") or {})
        base = dict(context.get("partial_elements") or {})

        elements = merge_selection(base, selection, deps)
        intent = _infer_intent(str(context.get("intent") or ""), elements)
        original_query = str(context.get("original_query") or "").strip()

        logger.info(
            "澄清提交续跑：metrics=%s（未收录 %s）dims=%s time=%s intent=%s",
            elements.get("metrics"),
            elements.get("unrecognized_metrics"),
            elements.get("dimensions"),
            elements.get("time_range"),
            intent,
        )

        out: Dict[str, Any] = {
            "query_elements": elements,
            "intent": intent,
            "clarification_resolved": True,
        }
        if original_query:
            out["query_original"] = original_query
        extra = str(selection.get("extra") or "").strip()
        if extra:
            # 自由补充说明仅留痕（供人工排查），不参与确定性编译，避免误改口径；
            # 需要自然语言补充时，前端会改走常规链路（LLM 抽取），不经过本节点。
            logger.info("澄清提交附带补充说明：%s", extra[:200])
        return out

    return apply_clarification
