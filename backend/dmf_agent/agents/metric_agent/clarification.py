"""结构化澄清载荷（WorkBuddy 式确认表单）。

背景：FR4 反问澄清原先只产出**自然语言追问**，用户必须自己重新组织语言（如
"存款余额，按分行，近 7 天"），既容易说错、也丢掉了系统已经识别出来的信息。
本模块把追问升级为**结构化确认**：系统给出候选（指标 / 维度 / 时间范围），用户在
界面上点选确认，提交后由 `apply_clarification` 节点**确定性合并**并继续执行后续
取数步骤（校验 -> 编译 -> 取数 -> 分析 -> 答复）。

候选**按置信度排名**（用户可见）：
- 指标：走既有 `MetricRecaller` 三路召回（无 LLM），用加权融合分 `RecalledItem.score`
  排序，并标注命中来源（完全匹配 / 黑话命中 / 模糊匹配）；
- 维度：用 `DimensionRecaller` 的 `exact_mapping` / `string_mapping_high` / `chat_map_names`
  分档折算置信度排序；候选**限定为已选指标支持的维度**，避免用户选出必然失败的维度；
- 时间范围：预设区间无置信度概念，保持固定顺序，只做已识别区间的回填。

设计原则：
- **候选只来自语义层**（真实指标名/别名、指标支持的维度、时间预设），不杜撰；
- **预选值取系统已识别的要素**（含"用户提到但未收录"的指标名），让用户**改**而不是重填；
- 纯函数、零 LLM 调用：反问路径不额外增加时延与不确定性。

载荷结构（前端 `ClarifyForm.vue` 消费）::

    {
      "type": "clarify",
      "title": "请确认查询要素",
      "rank_by": "confidence",
      "problems": [...],            # 触发澄清的原因（人工可读）
      "original_query": "...",      # 本轮已组合问句（提交后继续沿用）
      "fields": [
        {"key": "metrics", "label": "指标", "multiple": false, "required": true,
         "options": [{"value": "存款余额", "label": "存款余额",
                      "score": 1.0, "source": "full", "desc": "完全匹配"}],
         "selected": ["存款余额"], "allow_custom": true, "hint": "..."},
        ...
      ],
      "extra": {"key": "extra", "label": "补充说明（可选）", "placeholder": "..."}
    }
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, user_query

logger = logging.getLogger(__name__)

# 候选上限（避免长候选把卡片撑爆；用户仍可通过自定义输入指定其它指标/维度）
_MAX_METRIC_OPTIONS = 8
_MAX_DIMENSION_OPTIONS = 12

_CLARIFY_TITLE = "请确认查询要素"

# 召回来源 -> 人工可读标签（指标候选）
_SOURCE_LABELS = {
    "full": "完全匹配",
    "black_talk": "黑话命中",
    "part": "模糊匹配",
    "embed": "语义召回",
    "exact": "精确命中",
    "selected": "已识别",
    "candidate": "候选",
}

# 时间范围预设（value 由 `resolve_time_preset` 解析为 {start, end, granularity}）
TIME_PRESETS: List[Dict[str, str]] = [
    {"value": "last_7_days", "label": "近 7 天"},
    {"value": "last_30_days", "label": "近 30 天"},
    {"value": "this_month", "label": "本月"},
    {"value": "last_month", "label": "上月"},
    {"value": "last_3_months", "label": "近 3 个月"},
]

# 已识别（但非召回命中）项在排序中的兜底分：排在所有真实命中之后
_UNRANKED = -1.0


def shift_months(day: date, months: int) -> date:
    """按自然月平移日期（不引入 dateutil 依赖；日超出目标月天数时取该月最后一天）。"""
    total = (day.year * 12 + (day.month - 1)) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def resolve_time_preset(value: str, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """把时间预设 value 解析为 `{start, end, granularity}`；未知值返回 None。"""
    ref = today or date.today()
    key = str(value or "").strip()
    if key == "last_7_days":
        start, end = ref - timedelta(days=6), ref
    elif key == "last_30_days":
        start, end = ref - timedelta(days=29), ref
    elif key == "this_month":
        start, end = ref.replace(day=1), ref
    elif key == "last_month":
        first_this_month = ref.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "last_3_months":
        start, end = shift_months(ref.replace(day=1), -2), ref
    else:
        return None
    return {"start": start.isoformat(), "end": end.isoformat(), "granularity": "day"}


def match_time_preset(time_range: Optional[Dict[str, Any]], today: Optional[date] = None) -> str:
    """反查已解析时间区间对应的预设 value（匹配不到返回空串，前端走自定义展示）。"""
    tr = time_range or {}
    if not (tr.get("start") and tr.get("end")):
        return ""
    for item in TIME_PRESETS:
        resolved = resolve_time_preset(item["value"], today)
        if resolved and resolved["start"] == tr.get("start") and resolved["end"] == tr.get("end"):
            return item["value"]
    return ""


# 随载荷回传、提交时原样带回的要素字段（保留系统已识别的排序/同环比/阈值等，
# 用户在表单上只需改指标/维度/时间，不必丢失其它识别结果）
_PARTIAL_KEYS = (
    "metrics",
    "unrecognized_metrics",
    "dimensions",
    "filters",
    "metric_filters",
    "time_range",
    "order_by",
    "limit",
    "period_comparison",
    "period_comparison_type",
)


def _partial_elements(elements: Dict[str, Any]) -> Dict[str, Any]:
    """抽出需要在提交时保留的已识别要素（去掉空值，避免载荷冗余）。"""
    return {
        key: elements[key]
        for key in _PARTIAL_KEYS
        if elements.get(key) not in (None, "", [], {})
    }


def _query_text(state: AgentState) -> str:
    """本轮用于候选召回的文本：优先已组合问句（多轮澄清累计），否则规范问句。"""
    combined = state.get("confirm_combined")
    if combined:
        return str(combined)
    return user_query(state)


def _selected_metrics(elements: Dict[str, Any], state: AgentState) -> List[str]:
    """已识别指标（含"用户提到但未收录"的名字），作为表单预选值。"""
    out: List[str] = []
    for name in (
        list(elements.get("metrics") or [])
        + list((state.get("validation") or {}).get("missed_metrics") or [])
        + list(elements.get("unrecognized_metrics") or [])
    ):
        text = str(name).strip()
        if text and text not in out:
            out.append(text)
    return out


def _option(
    value: str, score: Optional[float], source: str, desc: str = ""
) -> Dict[str, Any]:
    base = _SOURCE_LABELS.get(source, "候选")
    if score is None:
        label = base
    else:
        label = f"{base}（置信度 {score:.2f}）"
    if desc:
        label = f"{label} · {desc}"
    return {"value": value, "label": value, "score": score, "source": source, "desc": label}


def _sort_by_confidence(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按置信度降序排序（无置信度项置底，同分保持原顺序，稳定）。"""
    return sorted(
        options,
        key=lambda o: (
            o.get("score") is None,
            -(o["score"] if isinstance(o.get("score"), (int, float)) else _UNRANKED),
        ),
    )


def _metric_options(
    deps: AgentDeps, text: str, selected: List[str]
) -> List[Dict[str, Any]]:
    """指标候选：语义层召回（无 LLM）-> 按加权置信度降序；已识别值兜底入列。"""
    layer = deps.semantic_layer
    recalled: List[Tuple[str, float, str]] = []
    if layer is not None:
        try:
            keys = layer.metric_search_keys()
            if deps.recaller is not None:
                for item in deps.recaller.recall(text, keys) or []:
                    resolved = layer.resolve_metric(str(item.name))
                    if resolved is not None:
                        recalled.append((resolved.name, float(item.score), str(item.matched_by)))
            else:
                # 无召回器：退化为字面命中（完全匹配），置信度取满分
                recalled = [(k, 1.0, "full") for k in dict.fromkeys(keys) if k and k in text]
        except Exception as exc:  # noqa: BLE001 - 召回失败不应阻断澄清
            logger.warning("澄清候选指标召回失败（降级为空候选）：%s", exc)

    options: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for name, score, source in recalled:
        if name in seen:
            continue
        seen.add(name)
        metric = layer.resolve_metric(name) if layer is not None else None
        alias_desc = f"别名：{'、'.join(metric.aliases)}" if metric and metric.aliases else ""
        options.append(_option(name, score, source, alias_desc))
    options = _sort_by_confidence(options)[:_MAX_METRIC_OPTIONS]

    # 兜底候选：召回不足时补上语义层指标（无置信度 -> 置底）。
    # 否则"问句什么都匹配不上"（如"统计一下情况怎么样"）时表单候选为空，用户只能手打。
    if layer is not None and len(options) < _MAX_METRIC_OPTIONS:
        # 兜底候选按**指标名排序**：候选与清单文件的物理布局无关
        # （多文件夹重组 / YAML→DB 切换都不改变默认候选，避免行为随文件命名漂移）
        for metric in sorted(layer.list_metrics(), key=lambda m: m.name):
            if len(options) >= _MAX_METRIC_OPTIONS:
                break
            if metric.name in seen:
                continue
            seen.add(metric.name)
            alias_desc = f"别名：{'、'.join(metric.aliases)}" if metric.aliases else ""
            options.append(_option(metric.name, None, "candidate", alias_desc))

    # 已识别但未被召回的（如"未收录"的名字）：置底兜底，仅作回填与提示
    ranked_names = {o["value"] for o in options}
    for name in selected:
        if name in ranked_names or name in seen:
            continue
        metric = layer.resolve_metric(name) if layer is not None else None
        desc = "未收录，选中后可发起创建" if (layer is not None and metric is None) else "已识别"
        options.append(_option(name, None, "selected", desc))
    return options


def _dimension_options(
    deps: AgentDeps, selected_metrics: List[str], selected_dims: List[str], text: str
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """维度候选：限定为已选指标支持的维度，按召回置信度降序。

    返回 `(options, valid_selected, invalid_selected)`：

    - `options` 只含**该指标真正支持**的维度（避免用户选出必然失败的组合）；
    - `valid_selected` 为可安全回填的预选值；
    - `invalid_selected` 为系统已识别、但该指标不支持、**已被剔除**的维度——
      必须显式告诉用户（曾经的问题：把不支持的维度留在候选中，用户以为改选了维度，
      提交后仍被拒、反复澄清）。
    """
    layer = deps.semantic_layer
    time_dim = layer.time_dimension_name() if layer is not None else None

    supported: List[str] = []
    if layer is not None:
        for name in selected_metrics:
            metric = layer.resolve_metric(name)
            if metric is None:
                continue
            for dim in layer.supported_dimensions(metric):
                if dim != time_dim and dim not in supported:
                    supported.append(dim)
    # 指标不可解析（如全部未收录）时退回全局维度，保证表单仍可用
    universe = supported or [
        d for d in (layer.dimension_names() if layer is not None else []) if d != time_dim
    ]

    valid_selected = [d for d in selected_dims if d in universe]
    invalid_selected = [d for d in selected_dims if d not in universe]

    scores: Dict[str, Tuple[float, str]] = {}
    if deps.dim_recaller is not None and universe:
        try:
            res = deps.dim_recaller.recall(text, universe)
            for name in res.exact_mapping:
                scores[name] = (1.0, "exact")
            for name in res.string_mapping_high:
                scores.setdefault(name, (0.8, "part"))
            for name in res.chat_map_names:
                scores.setdefault(name, (0.6, "part"))
        except Exception as exc:  # noqa: BLE001 - 召回失败不应阻断澄清
            logger.warning("澄清候选维度召回失败（按原顺序展示）：%s", exc)

    options: List[Dict[str, Any]] = []
    for name in universe:
        score, source = scores.get(name, (None, "candidate"))
        options.append(_option(name, score, source))
    return _sort_by_confidence(options)[:_MAX_DIMENSION_OPTIONS], valid_selected, invalid_selected


def _time_field(elements: Dict[str, Any]) -> Dict[str, Any]:
    time_range = elements.get("time_range") or {}
    preset = match_time_preset(time_range)
    label = ""
    if time_range.get("start") and time_range.get("end"):
        label = f"{time_range['start']} ~ {time_range['end']}"
    return {
        "key": "time_range",
        "label": "时间范围",
        "multiple": False,
        "required": False,
        "type": "single",
        "options": [dict(item) for item in TIME_PRESETS],
        "selected": [preset] if preset else [],
        "allow_custom": True,
        "hint": f"当前识别：{label}，如需其它区间可直接输入" if label else "选择或输入时间区间",
    }


def _dimension_hint(selected_metrics: Iterable[str], invalid_dims: Iterable[str]) -> str:
    """维度字段提示：说明可选范围（被剔除的不支持维度由 `invalid` 单独下发，前端红字提示）。"""
    names = [str(m).strip() for m in selected_metrics if str(m).strip()]
    if names:
        return f"仅可选「{'、'.join(names)}」支持的维度；不选则查全量汇总"
    return "按哪些维度拆分数据；不选则查全量汇总"


def _metric_hint(selected: Iterable[str]) -> str:
    names = [str(n) for n in selected if str(n).strip()]
    if not names:
        return "选择要查询的指标，或直接输入指标名（未收录的指标可发起创建）"
    return f"已识别：{'、'.join(names)}；可直接确认或改选其它指标"


def build_clarification(
    deps: AgentDeps,
    state: AgentState,
    problems: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """生成结构化澄清载荷（供前端渲染确认表单）。

    候选全部来自语义层真实元数据，**按置信度降序**；已识别要素作为预选值回填，
    用户只需修改/确认即可提交。
    """
    elements = state.get("query_elements") or {}
    text = _query_text(state)
    selected_metrics = _selected_metrics(elements, state)
    selected_dims = [str(d).strip() for d in (elements.get("dimensions") or []) if str(d).strip()]
    dim_options, dim_valid, dim_invalid = _dimension_options(
        deps, selected_metrics, selected_dims, text
    )

    fields: List[Dict[str, Any]] = [
        {
            "key": "metrics",
            "label": "指标",
            "multiple": False,
            "required": True,
            "type": "single",
            "options": _metric_options(deps, text, selected_metrics),
            "selected": selected_metrics[:1],
            "allow_custom": True,
            "hint": _metric_hint(selected_metrics),
        },
        {
            "key": "dimensions",
            "label": "维度（可多选）",
            "multiple": True,
            "required": False,
            "type": "multi",
            "options": dim_options,
            # 只回填该指标支持的维度；不支持的预选值单独放到 invalid 里提示用户
            "selected": dim_valid,
            "invalid": dim_invalid,
            "allow_custom": True,
            "hint": _dimension_hint(selected_metrics, dim_invalid),
        },
        _time_field(elements),
    ]

    original_query = str(state.get("confirm_combined") or state.get("query_original") or "")
    return {
        "type": "clarify",
        "title": _CLARIFY_TITLE,
        # 候选排序口径（前端可展示"按匹配置信度排序"）
        "rank_by": "confidence",
        "problems": list(problems or (state.get("validation") or {}).get("problems") or []),
        "original_query": original_query,
        # 提交时原样带回（前端透传）：保留本轮已识别要素与意图，供 apply_clarification 确定性合并
        "context": {
            "original_query": original_query,
            "intent": str(state.get("intent") or ""),
            "partial_elements": _partial_elements(elements),
        },
        "fields": fields,
        "extra": {
            "key": "extra",
            "label": "补充说明（可选）",
            "placeholder": "如：只看长三角、排除测试客户、按周汇总……",
        },
        "submit_label": "确认并继续",
    }


_TIME_REDIRECT_TITLE = "该时间段暂无数据，是否改查最新数据？"


def build_time_redirect_clarification(
    state: AgentState,
    diagnosis: Dict[str, Any],
) -> Dict[str, Any]:
    """空数据（查询时间晚于数据截至日）后的**时间窗重定向**确认载荷（FR5 闭环）。

    与要素确认表单（build_clarification）走同一套提交/续跑机制，但形态是一次
    「是否改查最新数据」的二选一确认：调整后窗口由后端确定性算好（见
    empty_trace.compute_time_redirect），前端只渲染说明与两个按钮。
    提交后由 apply_clarification 的 time_redirect 分支直接采用 adjusted 窗口。
    """
    redirect = (diagnosis or {}).get("redirect") or {}
    elements = state.get("query_elements") or {}
    # 续跑基线：本轮已识别要素（指标/维度/过滤等）+ 已重定向标记（防二次弹卡）
    partial = _partial_elements(elements)
    partial["time_redirected"] = True
    original_query = str(
        state.get("confirm_combined") or state.get("query_original") or user_query(state)
    )
    original = redirect.get("original") or {}
    adjusted = redirect.get("adjusted") or {}
    problems = [
        f"查询时间窗 {original.get('start') or '不限'} ~ {original.get('end') or '不限'} "
        f"晚于数据截至日期 {redirect.get('data_as_of')}，该时间段暂无数据"
    ]
    return {
        "type": "clarify_time_redirect",
        "title": _TIME_REDIRECT_TITLE,
        "problems": problems,
        "original_query": original_query,
        "data_as_of": redirect.get("data_as_of"),
        "original": original,
        "adjusted": adjusted,
        "window_kind": redirect.get("window_kind"),
        "adjusted_label": redirect.get("label"),
        # 提交时原样带回：apply_clarification 据此续跑（time_redirect=latest + adjusted 窗口）
        "context": {
            "original_query": original_query,
            "intent": str(state.get("intent") or ""),
            "partial_elements": partial,
        },
        "submit_label": "查询最新数据",
        "cancel_label": "暂不重查",
    }
