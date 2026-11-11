"""要素抽取节点（指标 / 维度 / 过滤 / 时间 / 排序 / 同环比 N 合一）。

候选集来源已切换到**语义层**（manifest.yml），不再依赖指标平台：
- 指标候选：语义层指标名 + 别名 -> MetricRecaller 三路召回
- 维度候选：语义层维度名 -> DimensionRecaller 召回
- 过滤候选：黑话词典（cant_dict）+ 可选 StarRocks 维值 DISTINCT 反查
  （DMF_STARROCKS-ENABLE_DIM_VALUE_RECALL=true 时启用）
- 时间：独立 time 模块（LLM 通道优先 + 规则兜底）

LLM 不可用时降级为规则抽取（只用真实语义层与真实时间解析，不产生假数据）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.sorting import (
    detect_rank_scope,
    normalize_llm_order,
    parse_sort,
)
from dmf_agent.agents.metric_agent.threshold import parse_metric_filters
from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.schemas.agent import QueryElements
from dmf_agent.time import parse_time_combined

logger = logging.getLogger(__name__)

# 维值反查最多查几个维度（避免一次抽取打太多查询）
_MAX_DIM_VALUE_LOOKUP = 3


async def _recall_candidates(
    text: str, deps: AgentDeps
) -> Tuple[List[str], List[str], List[dict]]:
    """候选召回（指标 / 维度 / 过滤），全部以语义层为元信息源。"""
    metrics: List[str] = []
    dimensions: List[str] = []
    filters: List[dict] = []

    layer = deps.semantic_layer
    if layer is not None:
        metric_list = layer.list_metrics()
        search_keys = layer.metric_search_keys()
        if deps.recaller is not None:
            recalled = deps.recaller.recall(text, search_keys)
            for item in recalled:
                metric = layer.resolve_metric(str(item.name))
                if metric is not None and metric.name not in metrics:
                    metrics.append(metric.name)
        else:
            metrics = [
                m.name
                for m in metric_list
                if m.name in text or any(alias in text for alias in m.aliases)
            ]

        dim_names = layer.dimension_names()
        if deps.dim_recaller is not None:
            dimensions = deps.dim_recaller.recall(text, dim_names).dimensions
        else:
            dimensions = [d for d in dim_names if d in text]

    filters.extend(await _recall_filter_values(text, deps, dimensions))

    if deps.cant_dict is not None:
        # 黑话维度/维值（走完全匹配，老 get_cant_candidates）
        cant = deps.cant_dict.recall(text)
        for dim in cant.dimensions:
            if dim not in dimensions:
                dimensions.append(dim)
        existing = {
            (f.get("dimension"), tuple(f.get("dimension_value") or [])) for f in filters
        }
        for f in cant.filters:
            key = (f.get("dimension"), tuple(f.get("dimension_value") or []))
            if key not in existing:
                filters.append(f)
                existing.add(key)
    return metrics, dimensions, filters


async def _recall_filter_values(
    text: str, deps: AgentDeps, dimensions: List[str]
) -> List[dict]:
    """过滤条件候选：从 StarRocks 取维度维值 DISTINCT 后打分召回。

    开关关闭、语义引擎不可用或查询失败时返回空列表（不阻断抽取）。
    """
    settings = deps.settings
    engine = deps.metric_engine
    if (
        engine is None
        or deps.filter_recaller is None
        or not settings.starrocks.enable_dim_value_recall
    ):
        return []

    layer = deps.semantic_layer
    targets = list(dimensions)
    if layer is not None and not targets:
        targets = [
            name
            for name in layer.dimension_names()
            if name != layer.time_dimension_name()
        ][:_MAX_DIM_VALUE_LOOKUP]
    targets = [d for d in targets if d][:_MAX_DIM_VALUE_LOOKUP]
    if not targets:
        return []

    rows: List[dict] = []
    for dim_name in targets:
        try:
            values = await engine.dimension_values(dim_name)
        except Exception as exc:  # noqa: BLE001 - 维值反查失败不影响主链路
            logger.warning("维值反查失败（维度=%s）：%s", dim_name, exc)
            continue
        rows.extend(
            {"dimCnName": dim_name, "dimValue": value, "match": True} for value in values
        )
    if not rows:
        return []
    return deps.filter_recaller.recall(text, rows).filters


async def _resolve_time_range(text: str, deps: AgentDeps) -> Dict[str, Any]:
    """时间解析（独立 time 模块，LLM 通道优先、规则兜底）；未识别时回退默认近 7 天。"""
    parsed = await parse_time_combined(text, get_chat_model=deps.get_chat_model)
    time_range = parsed.to_elements()
    if time_range is None:
        today = date.today()
        time_range = {
            "start": (today - timedelta(days=6)).isoformat(),
            "end": today.isoformat(),
            "granularity": "day",
        }
    return time_range


def format_candidates(
    text: str, metrics: List[str], dimensions: List[str], filters: List[dict]
) -> str:
    """组装候选集注入的 human 消息（迁移自老 prompt_element.py::get_prompt_input）。"""
    filter_candidates = [
        {"field": f.get("dimension"), "values": f.get("dimension_value") or []}
        for f in filters
    ]
    return (
        f"候选指标集: {metrics}\n"
        f"候选维度集: {dimensions}\n"
        f"候选过滤条件集: {filter_candidates}\n"
        f"用户输入的问题: {text}\n"
        f"输出："
    )


def format_preferences(state: AgentState) -> str:
    """把 L2 用户偏好渲染为要素抽取的"默认值"提示。

    仅在用户**未明确指定**时生效（例如用户没说粒度，就按偏好用月粒度）；
    用户当场说明的口径永远优先。无偏好时返回空串（不改动 prompt）。
    """
    prefs = state.get("preferences") or []
    lines = [f"- {p.get('key')}：{p.get('value')}" for p in prefs if p.get("value")]
    if not lines:
        return ""
    return (
        "用户偏好（仅当用户未明确指定时用作默认值，用户明确说明优先于偏好）：\n"
        + "\n".join(lines)
        + "\n"
    )


# 同环比短语 -> 类型（顺序无关；匹配时按「最长短语优先 + 非重叠」消费，
# 避免"周同比"同时命中 wow 与 yoy）
_POP_PHRASES = (
    ("周同比", "wow"),
    ("季同比", "qoq"), ("季度同比", "qoq"), ("年季环比", "qoq"),
    ("较上季", "qoq"), ("与上季", "qoq"), ("比上季", "qoq"), ("相比上季", "qoq"),
    ("环比上季", "qoq"), ("较上一季度", "qoq"), ("上季度环比", "qoq"), ("上季比", "qoq"),
    ("同比", "yoy"), ("去年同期", "yoy"), ("较去年", "yoy"), ("与去年", "yoy"),
    ("比去年", "yoy"), ("去年比", "yoy"), ("同期比", "yoy"), ("上年同期", "yoy"),
    ("较上年", "yoy"), ("与上年", "yoy"), ("比上年", "yoy"), ("年同比", "yoy"),
    ("环比", "mom"), ("月环比", "mom"), ("周环比", "mom"), ("日环比", "mom"),
    ("较上月", "mom"), ("与上月", "mom"), ("比上月", "mom"), ("上月比", "mom"),
    ("相比上月", "mom"), ("环比上月", "mom"), ("较上一月", "mom"),
    ("较上周", "mom"), ("与上周", "mom"), ("比上周", "mom"), ("上周比", "mom"),
    ("相比上周", "mom"),
)
POP_TYPES_ALLOWED = ("yoy", "mom", "qoq", "wow", "pop")
# 规范化展示顺序：同比在前、环比在后（口径稳定，避免 LLM/规则顺序不同导致列名抖动）
_POP_ORDER = {t: i for i, t in enumerate(POP_TYPES_ALLOWED)}


def _canonical_pop_order(types: List[str]) -> List[str]:
    """同环比类型去重 + 规范排序（yoy -> mom -> qoq -> wow -> pop）。"""
    out: List[str] = []
    for t in types:
        tt = str(t or "").strip().lower()
        if tt in POP_TYPES_ALLOWED and tt not in out:
            out.append(tt)
    return sorted(out, key=lambda t: _POP_ORDER.get(t, 9))


def _infer_pop_types(text: str) -> List[str]:
    """收集问句中**全部**同环比类型（FR1：同比 + 环比可并存）。

    按最长短语优先做非重叠消费："周同比"整体算 wow，不再被内部的"同比"重复记为 yoy。
    """
    if not text:
        return []
    consumed = [False] * len(text)
    found: List[str] = []
    for phrase, pop_type in sorted(_POP_PHRASES, key=lambda p: -len(p[0])):
        start = 0
        while True:
            idx = text.find(phrase, start)
            if idx < 0:
                break
            if not any(consumed[idx: idx + len(phrase)]):
                for i in range(idx, idx + len(phrase)):
                    consumed[i] = True
                if pop_type not in found:
                    found.append(pop_type)
            start = idx + 1
    return _canonical_pop_order(found)


def _infer_pop_type(text: str) -> Optional[str]:
    """从 query 推断同环比类型（LLM 不可用时的规则兜底），按特异性从高到低匹配。

    wow 周同比 / qoq 季同比 / yoy 同比 / mom 环比；除"同比/环比"关键词外，
    补充"较上月/与去年同期/相比上季度"等比较短语。
    多类型问句（同比 + 环比）返回规范顺序中的第一个，完整列表见 `_infer_pop_types`。
    """
    types = _infer_pop_types(text)
    return types[0] if types else None



def _element_confidence(text: str, metrics: List[str], deps: AgentDeps) -> float:
    """规则路径的确定性要素置信度（mock 可断言）。

    用户字面提到指标名/别名 -> 高（0.9）；仅模糊召回命中 -> 中（0.55）；无指标 -> 0.2。
    """
    if not metrics:
        return 0.2
    layer = deps.semantic_layer
    for name in metrics:
        metric = layer.resolve_metric(name) if layer is not None else None
        aliases = metric.aliases if metric is not None else []
        if name in text or any(a in text for a in aliases):
            return 0.9
    return 0.55


def _resolve_order(
    text: str,
    selected_metrics: List[str],
    raw_order: Optional[str],
    raw_limit: Any,
) -> Tuple[Optional[str], Optional[int]]:
    """排序归一：优先采用 LLM 给出的合法指标列，否则回退中文规则解析；limit 合并去重。"""
    order_by, limit = normalize_llm_order(raw_order, selected_metrics)
    if order_by is None:
        spec = parse_sort(text, selected_metrics)
        if spec is not None:
            order_by = spec.order_by
            if limit is None:
                limit = spec.limit
    if isinstance(raw_limit, int) and raw_limit >= 1:
        limit = raw_limit
    return order_by, limit


def _format_context_hint(state: AgentState) -> str:
    """把上一轮解析实体格式化为 LLM 要素抽取提示（P0-6 确定性指代消解）。

    不新增 LLM 调用：仅把 `ChatService` 注入的 `context_entities` 明确告诉抽取节点，
    「它 / 这个指标 / 按地区拆分 / 和上个月比」可沿用，而非只靠模糊改写。
    """
    ctx = state.get("context_entities")
    if not isinstance(ctx, dict):
        return ""
    parts: List[str] = []
    if ctx.get("active_metrics"):
        parts.append("上一轮查询的指标：" + "、".join(str(m) for m in ctx["active_metrics"]))
    if ctx.get("active_dimensions"):
        parts.append("上一轮维度：" + "、".join(str(d) for d in ctx["active_dimensions"]))
    tr = ctx.get("time_range") or {}
    if tr.get("granularity"):
        parts.append(f"上一轮时间粒度：{tr['granularity']}")
    if ctx.get("topic"):
        parts.append("当前主题：" + str(ctx["topic"]))
    if not parts:
        return ""
    return (
        "\n（多轮对话上下文："
        + "；".join(parts)
        + "。若本轮为追问（含\"它/这个/上述/按…拆分/同环比\"等）且未显式指定，"
        + "请沿用上述实体作为默认，仅覆盖用户本轮明确重述的部分。）\n"
    )


def _merge_context_defaults(elements: Dict[str, Any], state: AgentState) -> Dict[str, Any]:
    """规则路径下的上下文兜底：用户没指定指标/维度/过滤/时间时，沿用上一轮实体。

    只补**空缺**字段，不覆盖已解析出的值（与 LLM 路径的"明确说明优先"一致）。
    """
    ctx = state.get("context_entities")
    if not isinstance(ctx, dict):
        return elements
    if ctx.get("active_metrics") and not elements.get("metrics"):
        elements["metrics"] = [str(m) for m in ctx["active_metrics"]]
    if ctx.get("active_dimensions") and not elements.get("dimensions"):
        elements["dimensions"] = [str(d) for d in ctx["active_dimensions"]]
    tr = dict(elements.get("time_range") or {})
    for k, v in (ctx.get("time_range") or {}).items():
        if not tr.get(k):
            tr[k] = v
    elements["time_range"] = tr
    if ctx.get("active_filters") and not elements.get("filters"):
        # 对话状态里 filters 归一为 {字段: {op, values}}，这里还原为条件对象列表
        elements["filters"] = [
            {"field": str(field), "operator": str(spec.get("op", "=")),
             "values": list(spec.get("values") or [])}
            for field, spec in ctx["active_filters"].items()
            if isinstance(spec, dict)
        ]
    return elements



async def _rule_based_extract(text: str, deps: AgentDeps, state: Optional[AgentState] = None) -> Dict[str, Any]:
    """LLM 不可用时的规则抽取：候选召回 + 时间解析（均为真实数据，非造假）。"""
    metrics, dimensions, filters = await _recall_candidates(text, deps)
    time_range = await _resolve_time_range(text, deps)
    sort_spec = parse_sort(text, metrics, dimensions)
    pop_types = _infer_pop_types(text)
    elements = {
        "metrics": metrics,
        # 规则路径无法判定"用户提到的名字是否收录"（召回只返回已收录项），
        # 保持键结构一致；未命中引导由 validate_elements 的通用分支兜底。
        "unrecognized_metrics": [],
        "dimensions": dimensions,
        "filters": filters,
        "metric_filters": parse_metric_filters(text, metrics),
        "time_range": time_range,
        "order_by": sort_spec.order_by if sort_spec else None,
        "limit": sort_spec.limit if sort_spec else None,
        # FR1 排名范围：组内排名（每期内部各自排名）vs 全局排名
        "rank_scope": detect_rank_scope(text),
        "confidence": _element_confidence(text, metrics, deps),
        "period_comparison": bool(pop_types),
        "period_comparison_type": pop_types[0] if pop_types else None,
        "period_comparison_types": pop_types,
    }
    # P0-6：规则路径也沿用上一轮上下文兜底（与 LLM 路径"明确优先、空缺沿用"一致）
    if state is not None:
        return _merge_context_defaults(elements, state)
    return elements


def _apply_preference_defaults(
    elements: Dict[str, Any], state: AgentState
) -> Dict[str, Any]:
    """规则路径下的 L2 偏好兜底：用户没指定粒度/维度时按偏好补默认值。

    只补**空缺**字段，不覆盖已解析出的值，与 LLM 路径的"明确说明优先"一致。
    """
    prefs = {p.get("key"): p.get("value") for p in (state.get("preferences") or [])}
    time_range = dict(elements.get("time_range") or {})
    if prefs.get("granularity") and not time_range.get("granularity"):
        time_range["granularity"] = str(prefs["granularity"])
        elements["time_range"] = time_range
    if prefs.get("dimensions") and not elements.get("dimensions"):
        raw = str(prefs["dimensions"])
        elements["dimensions"] = [d.strip() for d in raw.split(",") if d.strip()]
    return elements


def build_extract_elements_node(deps: AgentDeps):
    async def extract_elements(state: AgentState) -> Dict[str, Any]:
        # 改写后的规范问句（含多轮确认拼接：rewrite_query 的输入优先级已处理 confirm_combined）
        user_text = user_query(state)
        llm = deps.get_chat_model()
        if llm is None:
            elements = _apply_preference_defaults(
                await _rule_based_extract(user_text, deps, state), state
            )
            logger.info("[规则降级] 要素抽取：%s", elements)
            return {"query_elements": elements, "mock_mode": True}

        async def _primary() -> Dict[str, Any]:
            # 候选召回 -> 注入 human 消息（老系统 prompt_element 的候选集输入）
            metrics, dimensions, filters = await _recall_candidates(user_text, deps)
            result: QueryElements = await astructured_invoke(
                llm,
                QueryElements,
                [
                    SystemMessage(content=load_prompt("elements")["system"]),
                    HumanMessage(
                        content=(
                            format_preferences(state)
                            + format_candidates(user_text, metrics, dimensions, filters)
                            + _format_context_hint(state)
                        )
                    ),
                ],
            )
            elements = result.model_dump()
            # 排序归一：LLM 自由文本 order_by -> 规范 "<指标> asc|desc"，中文 TopN -> limit
            selected = elements.get("metrics") or []
            elements["order_by"], elements["limit"] = _resolve_order(
                user_text, selected, elements.get("order_by"), elements.get("limit")
            )
            # 排名范围：LLM 给了合法值就用，否则规则判定（"各分行每月…排名"-> group）
            raw_scope = str(elements.get("rank_scope") or "").strip().lower()
            elements["rank_scope"] = (
                raw_scope if raw_scope in ("global", "group") else detect_rank_scope(user_text)
            )
            # 置信度：LLM 未给合法值时用确定性启发补齐
            conf = elements.get("confidence")
            if not isinstance(conf, (int, float)) or not (0 <= float(conf) <= 1):
                elements["confidence"] = _element_confidence(user_text, selected, deps)
            # 同环比：LLM 单值 + 规则多值**合并**（FR1：同比 + 环比可并存），
            # 合并后统一规范顺序并回写 period_comparison_type（取首个）与布尔位。
            pops = _canonical_pop_order(
                list(elements.get("period_comparison_types") or [])
                + [elements.get("period_comparison_type") or ""]
                + _infer_pop_types(user_text)
            )
            elements["period_comparison_types"] = pops
            elements["period_comparison_type"] = pops[0] if pops else None
            elements["period_comparison"] = bool(pops) or bool(
                elements.get("period_comparison")
            )
            # 指标阈值：以 LLM 结构化为准，规则解析补齐其遗漏（中文万/亿归一确定性可靠）
            llm_thresholds = [
                (str(mf.get("metric")), str(mf.get("operator")))
                for mf in (elements.get("metric_filters") or [])
                if isinstance(mf, dict) and mf.get("metric")
            ]
            for mf in parse_metric_filters(user_text, selected):
                pair = (str(mf["metric"]), str(mf["operator"]))
                if pair not in llm_thresholds:
                    elements.setdefault("metric_filters", []).append(mf)
            # 时间由独立 time 模块解析（对齐规则路径），覆盖 LLM 输出
            elements["time_range"] = await _resolve_time_range(user_text, deps)
            logger.info("要素抽取：%s", elements)
            return {"query_elements": elements}

        async def _fallback() -> Dict[str, Any]:
            logger.warning("LLM 要素抽取失败，限次自愈：回退规则抽取")
            return {
                "query_elements": _apply_preference_defaults(
                    await _rule_based_extract(user_text, deps, state), state
                ),
                "mock_mode": True,
            }

        # 节点级回写错误样本池（only_recovered=True：自愈成功才采样，未自愈的失败
        # 会抛 ParseError 由 chat_service 统一采样，避免重复计数）
        ctx = {
            "session_id": state.get("session_id") or "",
            "user_id": state.get("user_id") or "",
            "tenant_id": state.get("tenant_id") or "",
            "round_id": state.get("round_id") or "",
            "node": "extract_elements",
            "trace_snippet": user_text[:500],
        }
        from dmf_agent.agents.metric_agent.recovery import (
            make_recovery_hook,
            recover_parse,
        )

        hook = make_recovery_hook(deps.error_sample_repo, ctx, only_recovered=True)
        return await recover_parse(
            "extract_elements", _primary, _fallback, deps.recovery_settings, on_recovery=hook
        )

    return extract_elements
