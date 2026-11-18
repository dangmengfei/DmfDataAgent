"""归因节点：主指标概况 + 维度值归因 + 规则总结（P1/P2 增强）。

迁移自老 plugin/diagnosis/ 的 Diagnosis 编排核心
（get_metric_info_output / get_dim_cause_data / get_contribution_list /
get_smart_summary_rule）。复用 execute_query 已取回的明细做纯 Python 归因，
输出写入 state.diagnosis 供 synthesize_answer 引用。

P1 增强（2026-11-12，见 doc/归因分析优化方案.md，DMF_DIAGNOSIS-* 可整体关闭回退旧行为）：
- 期间对比：本期（时间轴后半段）vs 上期（前半段）替代端点对比（compare_mode=endpoint
  时保留旧行为；时间点不足两个时自动回落端点逻辑）；
- 异动前置：先判定波动是否显著（significant/mild/noise/insufficient，纯 Python），
  noise 只输出趋势描述、跳过维度归因，避免把随机波动解释成业务原因；
- 自动补维度：取数结果缺时间列或没有任何维度分组时，从语义层取该指标可用维度
  自动补发一次 GROUP BY 查询（走 harness 白名单工具 `diagnose_fetch`），
  LLM 可规划优先维度（结构化输出），失败降级用原始数据、如实说明；
- 结构变化标注：维度成员新增（added）/消失（removed）单独列示，不与涨跌混排。

P2 增强（2026-11-16，方案 §6）：
- 两维交叉归因（`diagnosis/core.py::cross_attribution`）：结果含 ≥2 个业务维度且
  期间口径时，输出 (主维度 × 第二维度) 的交叉贡献矩阵——纯计算零取数；
- 递归下钻（`_drilldown`）：对贡献 TOP 维值用**新维度**再切一刀（depth ≤
  max_drill_depth，每层一次 `diagnose_fetch` 补取数，下钻取数 DSL 会**过滤到父维值**）；
  路径规划 LLM 优先（`diagnosis_plan` prompt + DiagnosisPlanStep，模型驱动），
  无 LLM/失败/非法动作回落规则兜底（固定一层：贡献最大维值 + 声明顺序首个候选维度）；
  仅在异动判定 significant 时触发（轻度波动不值得额外取数）；
- 行动建议接知识库（`_advice_docs`）：以「指标 + TOP 维值」调 harness 工具
  `retrieve_knowledge`，命中片段供答复组织建议；无命中/无权限/降级保留模板建议。

TODO(v2-migration) 遗留（见 MIGRATION Phase 4c）：
- 因子归因（DiagnosisMetric，需指标血缘公式元数据）—— P3
- 指标树相关性（DiagnosisTree，需 metrics_tree 表）—— P3
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.diagnosis.anomaly import detect_anomaly
from dmf_agent.diagnosis.core import (
    contribution_top,
    cross_attribution,
    dim_attribution,
    global_summary,
    metric_change,
    period_change,
    period_dim_attribution,
    period_global_summary,
    period_smart_summary,
    rows_to_dicts,
    smart_summary,
)
from dmf_agent.analysis import is_time_column
from dmf_agent.harness import HarnessContext, HarnessState

logger = logging.getLogger(__name__)

# 期间归因时 kept 维值明细最多保留条数（避免 node_trace 过大；TOP 由 contribution_top 截断）
_MAX_DIM_DETAIL = 50

# 归因取数 DSL 要剥离的键：order_by+LIMIT 会截断行（TopN 查询带 limit 时贡献度失真）、
# advanced 的聚合覆盖/占比/排名会改写输出列、period_comparison 会输出前期/增长值等
# 额外指标列、compare_mode 是归因内部口径标记（引擎不感知）。
_FETCH_DROP_KEYS = (
    "order_by",
    "limit",
    "rank_scope",
    "advanced",
    "period_comparison",
    "period_comparison_type",
    "period_comparison_types",
    "compare_mode",
)


def _attribution_fetch_dsl(
    dsl: Dict[str, Any],
    dims: List[str],
    extra_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """归因取数 DSL：沿用指标/过滤/时间窗，剥离排序/TopN/二次分析/同环比。

    归因需要该口径下的**全量分组明细**（先算后说的「算」的材料），任何会截行、
    改列、加列的指令都不带——下钻/补维度取数与首次自动补取数共用本函数。
    """
    out = {k: v for k, v in dsl.items() if k not in _FETCH_DROP_KEYS}
    out["dimensions"] = list(dims)
    filters = list(dsl.get("filters") or [])
    if extra_filters:
        filters = filters + [f for f in extra_filters if f]
    if filters:
        out["filters"] = filters
    return out


def _diagnose(
    query_result: Dict[str, Any],
    dsl: Dict[str, Any],
    multi_metric: bool = True,
) -> Dict[str, Any]:
    """纯归因计算（可单测）：返回 state.diagnosis 结构。

    P1 期间对比：compare_mode 由 DSL 上游（build_diagnose_result）写入 dsl["compare_mode"]；
    缺省 period，时间点不足 2 个自动回落 endpoint。

    多指标联合归因（2026-11-17）：问句带多个指标时，除主指标（metrics[0]，占顶层字段，
    向后兼容）外，其余指标在**同一份取数结果**上各自独立归因（期间/异动/维度归因均为
    纯计算，零额外取数），写入 per_metric；metric_relations 记录各指标与主指标的
    同向/背离关系。P2 下钻与 P3 因子/树仍只对主指标做（逐指标展开会让补取数次数爆炸）。
    """
    columns = query_result.get("columns") or []
    rows = rows_to_dicts(query_result)
    if not rows or not columns:
        return {"ok": False, "message": "无可用取数数据，无法进行归因"}

    compare_mode = str(dsl.get("compare_mode") or "period")
    time_col, has_time = _detect_time_column(columns, rows)
    requested = [m.get("name") for m in (dsl.get("metrics") or []) if m.get("name")]
    # 时间列不是业务维度：不能作为归因下钻对象（否则会输出「某日期为本期新增成员」这类
    # 无意义结论）。仅保留真实的业务维度列。
    dims = [
        d for d in (dsl.get("dimensions") or []) if d in columns and d != time_col
    ]

    present = list(dict.fromkeys(m for m in requested if m in columns))
    missing = [m for m in requested if m not in columns]
    if not present:
        return {"ok": False, "message": "取数结果中缺少指标列，无法进行归因"}

    if not has_time:
        # 无时间列：无法划分期间/端点，明确报缺（不猜、不编造）——
        # 真实编译器在 dimensions 不含时间维度时只输出维度+指标列，此时应由
        # 上游自动补时间取数（_fetch_diagnosis_data）拿到时间轴后再进本函数。
        return {
            "ok": False,
            "message": "取数结果缺少时间列，无法划分对比期间，无法进行归因",
            "needs_time_fetch": True,
        }

    use_period = compare_mode == "period"
    primary_name = present[0]
    primary = _diagnose_single(rows, time_col, primary_name, dims, use_period)
    if primary is None:
        return {
            "ok": False,
            "message": f"指标【{primary_name}】在取数范围内不足两个有数据的时间点，无法进行归因",
        }
    result = primary

    # 多指标联合归因：次要指标各自独立归因 + 与主指标的方向关系（零额外取数）
    if multi_metric and (len(present) > 1 or missing):
        secondaries: List[Dict[str, Any]] = []
        for name in present[1:]:
            single = _diagnose_single(rows, time_col, name, dims, use_period)
            if single is None:
                secondaries.append(
                    {
                        "ok": False,
                        "metric": name,
                        "message": f"指标【{name}】在取数范围内不足两个有数据的时间点，无法进行归因",
                    }
                )
            else:
                secondaries.append(_compact_metric_entry(single))
        for name in missing:
            secondaries.append(
                {"ok": False, "metric": name, "message": "取数结果中缺少该指标列"}
            )
        if secondaries:
            result["per_metric"] = secondaries
            result["metric_relations"] = _metric_relations(
                primary_name, primary.get("change"), secondaries
            )
    return result


def _diagnose_single(
    rows: List[Dict[str, Any]],
    time_col: str,
    metric: str,
    dims: List[str],
    use_period: bool,
) -> Optional[Dict[str, Any]]:
    """单指标的完整归因计算（期间/端点 + 异动 + 维度归因 + 规则总结）。

    时间点不足两个（期间与端点均不可算）返回 None；异动为 noise 时返回
    仅含趋势描述的结果（跳过维度归因）。主指标与次要指标共用本函数。
    """
    change: Optional[Dict[str, Any]] = None
    used_period = use_period
    if used_period:
        change = period_change(rows, time_col, metric)
        if change is None:
            # 期间划分不可行（< 2 个时间点）：回落端点口径
            used_period = False
    if not used_period:
        change = metric_change(rows, time_col, metric)
    if change is None:
        return None

    # 异动前置判定：本期时间点序列 + 期间变化率
    time_values = _time_point_totals(rows, time_col, metric)
    anomaly = detect_anomaly(time_values, change.get("change_rate"))

    # noise（波动处于正常范围）：只输出趋势描述，**不计算**维度归因——
    # 避免把随机波动解释成业务原因（问题清单 #2）。
    if anomaly["status"] == "noise":
        global_text = (
            period_global_summary(change) if used_period else global_summary(change)
        )
        return {
            "ok": True,
            "metric": metric,
            "global_summary": global_text,
            "change": change,
            "compare_mode": "period" if used_period else "endpoint",
            "anomaly": anomaly,
            "dimensions": {},
            "same_contribution_list": [],
            "opposite_contribution_list": [],
            "added_members": [],
            "removed_members": [],
            "smart_summary": f"{global_text}{anomaly['message']}",
            "primary_dim": None,
            "action_hints": [],
            "skipped_dimensions": list(dims),
        }

    dim_results: Dict[str, List[Dict[str, Any]]] = {}
    for dim in dims:
        if used_period:
            dim_results[dim] = period_dim_attribution(rows, time_col, dim, metric)
        else:
            dim_results[dim] = dim_attribution(rows, time_col, dim, metric)

    # 首个有结果的维度作为主维度，产出同向/反向 TOP 与规则总结
    primary_dim = next((d for d in dims if dim_results.get(d)), None)
    primary_results = dim_results.get(primary_dim, []) if primary_dim else []
    # added/removed 成员不参与同向/反向 TOP（单独列示，避免结构变化混入涨跌归因）
    kept_only = [r for r in primary_results if r.get("presence", "kept") == "kept"]
    contrib = contribution_top(kept_only, change.get("change_value"))

    if used_period:
        added = [r for r in primary_results if r.get("presence") == "added"]
        removed = [r for r in primary_results if r.get("presence") == "removed"]
        global_text = period_global_summary(change)
        summary = period_smart_summary(
            change, contrib["same"], contrib["opposite"], global_text, added, removed
        )
    else:
        added, removed = [], []
        global_text = global_summary(change)
        summary = smart_summary(change, contrib["same"], contrib["opposite"], global_text)

    return {
        "ok": True,
        "metric": metric,
        "global_summary": global_text,
        "change": change,
        "compare_mode": "period" if used_period else "endpoint",
        "anomaly": anomaly,
        "dimensions": _trim_dims(dim_results),
        "same_contribution_list": contrib["same"],
        "opposite_contribution_list": contrib["opposite"],
        "added_members": added,
        "removed_members": removed,
        "smart_summary": summary,
        "primary_dim": primary_dim,
        # FR3：基于确定性贡献 TOP 生成的行动建议线索（无知识库依赖，供 LLM 组织建议）
        "action_hints": _build_action_hints(
            change, contrib["same"], contrib["opposite"], primary_dim
        ),
    }


def _compact_metric_entry(single: Dict[str, Any], max_top: int = 3) -> Dict[str, Any]:
    """次要指标的紧凑归因结果（进 per_metric / trace；不带完整维度明细防膨胀）。"""
    anomaly = single.get("anomaly") or {}
    return {
        "ok": True,
        "metric": single.get("metric"),
        "compare_mode": single.get("compare_mode"),
        "change": single.get("change"),
        "anomaly": {"status": anomaly.get("status"), "message": anomaly.get("message")},
        "primary_dim": single.get("primary_dim"),
        "same_contribution_list": (single.get("same_contribution_list") or [])[:max_top],
        "opposite_contribution_list": (single.get("opposite_contribution_list") or [])[:max_top],
        "added_members": single.get("added_members") or [],
        "removed_members": single.get("removed_members") or [],
        "global_summary": single.get("global_summary", ""),
        "smart_summary": single.get("smart_summary", ""),
    }


def _metric_relations(
    primary_name: str,
    primary_change: Optional[Dict[str, Any]],
    secondaries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """各次要指标与主指标的**方向关系**（确定性判定，联合归因的「联」）。

    同向 = 与主指标变化方向一致；背离 = 方向相反；次要指标零变化记「持平」。
    只描述方向事实，不做因果推断（因果归 P3 因子/树归因）。
    """
    p_change = (primary_change or {}).get("change_value")
    if p_change is None:
        return []
    relations: List[Dict[str, Any]] = []
    for entry in secondaries:
        if not entry.get("ok"):
            continue
        s_change = (entry.get("change") or {}).get("change_value")
        if s_change is None:
            continue
        if s_change == 0:
            relation = "持平"
        else:
            relation = "同向" if (p_change > 0) == (s_change > 0) else "背离"
        relations.append(
            {
                "metric": entry.get("metric"),
                "change_value": s_change,
                "change_rate": (entry.get("change") or {}).get("change_rate"),
                "relation": relation,
                "vs": primary_name,
            }
        )
    return relations


def _detect_time_column(
    columns: List[str], rows: List[Dict[str, Any]]
) -> Tuple[Optional[str], bool]:
    """探测时间列：优先复用 analysis.stats 的识别规则（名称 + 日期样例）。

    返回 (列名, 是否找到)。真实编译器在 dimensions 不含时间维度时输出列为
    维度+指标（无时间列），此时返回 (None, False)。
    """
    for col in columns:
        values = [r.get(col) for r in rows if r.get(col) is not None]
        if is_time_column(col, values):
            return str(col), True
    return None, False


def _time_point_totals(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str
) -> List[float]:
    """按时间排序的各时间点指标总量（异动判定的输入序列）。"""
    totals: Dict[Any, float] = {}
    for r in rows:
        t = r.get(time_col)
        v = r.get(metric_col)
        if t is None or not isinstance(v, (int, float)):
            continue
        totals[t] = totals.get(t, 0.0) + v
    return [totals[t] for t in sorted(totals, key=str)]


def _trim_dims(dim_results: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """维值明细截断（每维度最多 _MAX_DIM_DETAIL 条，防大基数维度撑爆 node_trace）。"""
    return {d: items[:_MAX_DIM_DETAIL] for d, items in dim_results.items() if items}


def _build_action_hints(
    change: Dict[str, Any],
    same: List[Dict[str, Any]],
    opposite: List[Dict[str, Any]],
    primary_dim: Optional[str],
) -> List[str]:
    """基于确定性贡献 TOP 生成行动建议线索（FR3，无知识库依赖）。

    仅从已计算的归因结果组织可执行的排查/复用建议，不臆造数据外事实。
    """
    hints: List[str] = []
    if opposite:
        top = opposite[0]
        dim_desc = f"{top.get('dim_name')}={top.get('dim_value')}" if primary_dim else str(top)
        hints.append(
            f"优先排查反向贡献 TOP：{dim_desc}（影响值 {top.get('change_value')}），"
            f"定位其业务动作是否异常或存在外部冲击。"
        )
    if same:
        top = same[0]
        dim_desc = f"{top.get('dim_name')}={top.get('dim_value')}" if primary_dim else str(top)
        hints.append(
            f"复用同向贡献 TOP 经验：{dim_desc}（正向影响 {top.get('change_value')}），"
            f"评估能否复制到其它维度值以放大整体收益。"
        )
    if opposite and same:
        hints.append(
            "整体指标由同向维度值拉动、被反向维度值拖累，建议资源向正向单元倾斜、"
            "对反向单元做专项诊断。"
        )
    return hints


# ---------------- 自动补维度取数（P1） ----------------


async def _plan_dims(
    llm: Any, metric: str, available_dims: List[str], user_text: str
) -> List[str]:
    """规划归因下钻维度：LLM 优先（模型驱动），失败/无 LLM 按传入候选顺序兜底。

    返回截断到 3 个的维度名列表（已过滤不支持项）。LLM 只做「排序」，
    不产生候选之外的任何内容；返回非法值一律回落候选顺序（对齐 FR14 惯用法）。
    """
    if not available_dims:
        return []
    limit = 3
    candidate = available_dims[:limit]
    if llm is None:
        return candidate
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from dmf_agent.llm.structured import astructured_invoke
        from dmf_agent.schemas.agent import DiagnosisDimPlan

        plan: DiagnosisDimPlan = await astructured_invoke(
            llm,
            DiagnosisDimPlan,
            [
                SystemMessage(
                    content=(
                        "你是数据分析专家。用户在做指标归因，需要选择下钻维度。"
                        "请从候选维度中选出最能解释指标变化的 1~3 个维度，"
                        "按解释力从强到弱排序。只输出 JSON。"
                    )
                ),
                HumanMessage(
                    content=(
                        f"用户问题：{user_text}\n"
                        f"归因指标：{metric}\n"
                        f"候选维度：{available_dims}\n"
                        '输出格式：{"dimensions": ["维度1", "维度2"]}'
                    )
                ),
            ],
        )
        picked = [d for d in plan.dimensions if d in available_dims][:limit]
        return picked or candidate
    except Exception as exc:  # noqa: BLE001 - LLM 规划失败回落声明顺序
        logger.warning("归因维度规划 LLM 失败，回落声明顺序：%s", exc)
        return candidate


async def _fetch_diagnosis_data(
    state: AgentState,
    deps: AgentDeps,
    hstate: HarnessState,
    hctx: HarnessContext,
) -> Dict[str, Any]:
    """自动补时间/维度取数：走 harness 白名单工具 `diagnose_fetch`。

    触发条件（settings.diagnosis.auto_fetch_dims）：
    - 取数结果无可用时间列（真实编译器在 dimensions 不含时间维度时无时间轴）；
    - 或没有任何维度分组（用户问句未带维度，归因无米下锅）。

    补取数策略：在原 DSL 上**追加**维度（时间维度 + 语义层该指标的可用维度，
    LLM 可规划优先级），指标/过滤/时间窗沿用原 DSL（排序/TopN/二次分析剥离，
    见 _attribution_fetch_dsl）；重复取数同口径，保证可对比。
    取数失败 / 引擎不可用 / 开关关闭时返回原 query_result（降级不阻断，warnings 如实标注）。

    hstate/hctx 由节点创建并贯穿 P1/P2 全部补取数（预算累计与 execute_query 同口径）。
    """
    qr = state.get("query_result") or {}
    dsl = state.get("dsl") or {}
    ds = deps.diagnosis_settings
    warnings: List[str] = []
    if not ds.enabled or not ds.auto_fetch_dims:
        return qr

    columns = qr.get("columns") or []
    rows = rows_to_dicts(qr)
    time_col_name, has_time = _detect_time_column(columns, rows)
    layer = deps.semantic_layer
    time_dim = layer.time_dimension_name() if layer is not None else None
    # 业务维度 = dsl 声明且落在结果列里、且不是时间列（时间维度不算归因维度：
    # 「每天 GMV 为什么跌」只有时间轴时仍需要补业务维度下钻）
    business_dims = [
        d for d in (dsl.get("dimensions") or []) if d in columns and d != time_col_name
    ]

    if has_time and business_dims:
        return qr

    engine = deps.metric_engine
    if engine is None:
        if not has_time:
            warnings.append("语义引擎未装配，无法自动补充时间/维度取数")
            return {**qr, "_diagnosis_warnings": warnings}
        return qr

    if not time_dim:
        if not has_time:
            warnings.append("语义层未声明时间维度，无法补充时间轴取数")
            return {**qr, "_diagnosis_warnings": warnings}
        return qr

    # 可用维度 = 该指标支持维度 - 时间维度 - 已在结果中的维度
    metric_name = ""
    for m in dsl.get("metrics") or []:
        if isinstance(m, dict) and m.get("name"):
            metric_name = str(m["name"])
            break
    available: List[str] = []
    if layer is not None and metric_name:
        try:
            metric = layer.resolve_metric(metric_name)
            if metric is not None:
                available = [
                    d
                    for d in layer.supported_dimensions(metric)
                    if d != time_dim and d not in business_dims
                ]
        except Exception:  # noqa: BLE001 - 语义层解析失败按无候选处理
            available = []
    planned = await _plan_dims(
        deps.get_chat_model(), metric_name, available, user_query(state)
    )

    existing_dims = list(dsl.get("dimensions") or [])
    add_dims = ([time_dim] if not has_time else []) + [
        d for d in planned[: max(0, ds.max_auto_dims)] if d not in existing_dims
    ]
    if not add_dims:
        return qr

    new_dsl = _attribution_fetch_dsl(dsl, existing_dims + add_dims)
    harness = getattr(deps, "harness", None)

    async def _raw():
        return await engine.query(new_dsl)

    try:
        if harness is not None:
            fetched = await harness.invoke(
                "diagnose_fetch", {"dsl": new_dsl}, hctx, hstate, _raw
            )
        else:
            fetched = await _raw()
    except Exception as exc:  # noqa: BLE001 - 补取数失败降级用原始结果
        logger.warning("归因补维度取数失败，降级用原始取数结果：%s", exc)
        warnings.append(f"自动补充维度取数失败（{exc}），以下归因基于原始查询结果。")
        return {**qr, "_diagnosis_warnings": warnings}

    fetched_columns = fetched.get("columns") or []
    if not fetched.get("rows") or not fetched_columns:
        warnings.append("自动补充维度取数未返回数据，以下归因基于原始查询结果。")
        return {**qr, "_diagnosis_warnings": warnings}

    logger.info("归因补维度取数成功：追加维度=%s rows=%s", add_dims, fetched.get("row_count"))
    return {
        **fetched,
        "_diagnosis_warnings": warnings,
        "_diagnosis_fetch_dsl": new_dsl,
    }


# ---------------- P2：两维交叉归因（纯计算，零取数） ----------------


def _compute_cross(
    query_result: Dict[str, Any],
    dsl: Dict[str, Any],
    ds: Any,
    base_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """两维交叉归因：结果含 ≥2 个业务维度且期间口径时计算 (主维度 × 第二维度)。

    纯 Python 计算、不取数（材料已在手）；endpoint 口径 / 维度不足 / 开关关闭
    返回 None（跳过交叉段，不阻断）。
    """
    if not ds.enabled or not ds.cross_enabled:
        return None
    if not base_result.get("ok") or base_result.get("compare_mode") != "period":
        return None
    if (base_result.get("anomaly") or {}).get("status") not in ("significant", "mild"):
        return None
    columns = query_result.get("columns") or []
    rows = rows_to_dicts(query_result)
    if not rows:
        return None
    time_col, has_time = _detect_time_column(columns, rows)
    if not has_time:
        return None
    metric = base_result.get("metric")
    if not metric:
        return None
    dims = [d for d in (dsl.get("dimensions") or []) if d in columns and d != time_col]
    if len(dims) < 2:
        return None
    dim_a = base_result.get("primary_dim") or dims[0]
    if dim_a not in dims:
        dim_a = dims[0]
    dim_b = next((d for d in dims if d != dim_a), None)
    if dim_b is None:
        return None
    return cross_attribution(rows, time_col, dim_a, dim_b, metric, ds.max_cross_combos)


# ---------------- P2：递归下钻（LLM 规划优先 + 规则兜底） ----------------


def _rule_drill_step(
    parent_options: List[Dict[str, Any]], candidates: List[str], path: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """规则兜底下钻动作：只在**首层**执行（贡献最大维值 + 声明顺序首个候选维度）。

    parent_options 已按 |变化量| 降序（period_dim_attribution 的输出顺序），
    取首位即贡献最大的维值；更深层级没有确定性依据，不猜（返回 None = 停止）。
    """
    if path or not parent_options or not candidates:
        return None
    top = parent_options[0]
    return {
        "action": "drill",
        "parent_dim": str(top.get("dim_name") or ""),
        "value": top.get("dim_value"),
        "dim": candidates[0],
        "reason": "规则兜底：贡献最大维值 + 声明顺序首个候选维度",
    }


async def _plan_next_drill(
    llm: Any,
    state: AgentState,
    base_result: Dict[str, Any],
    path: List[Dict[str, Any]],
    parent_dim: str,
    parent_options: List[Dict[str, Any]],
    candidates: List[str],
) -> Optional[Dict[str, Any]]:
    """LLM 规划下一步下钻动作（diagnosis_plan prompt + DiagnosisPlanStep）。

    失败/无 LLM 返回 None（调用方走规则兜底）。返回的动作**未校验**，
    合法性（维度在候选内、维值在已知集合、父维度正确）由调用方判定。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from dmf_agent.agents.prompt_loader import load_prompt
    from dmf_agent.llm.structured import astructured_invoke
    from dmf_agent.schemas.agent import DiagnosisPlanStep

    primary_dim = base_result.get("primary_dim")
    primary_list = (base_result.get("dimensions") or {}).get(primary_dim) or []
    kept = [r for r in primary_list if r.get("presence", "kept") == "kept"]
    primary_summary = (
        "、".join(f"{r.get('dim_value')}={r.get('change_value')}" for r in kept[:5])
        or "（无）"
    )
    anomaly = base_result.get("anomaly") or {}
    if path:
        path_text = "\n".join(
            "{}={} → {}：{}".format(
                node.get("parent_dim"),
                node.get("parent_value"),
                node.get("dim"),
                "、".join(
                    f"{c.get('dim_value')}={c.get('change_value')}"
                    for c in (node.get("children") or [])[:3]
                )
                or "（无）",
            )
            for node in path
        )
    else:
        path_text = "（无）"
    option_values = [str(o.get("dim_value")) for o in parent_options[:10]]

    try:
        step: DiagnosisPlanStep = await astructured_invoke(
            llm,
            DiagnosisPlanStep,
            [
                SystemMessage(content=load_prompt("diagnosis_plan")["system"]),
                HumanMessage(
                    content=(
                        f"用户问题：{user_query(state)}\n"
                        f"异动判定：{anomaly.get('message', '')}\n"
                        f"主维度归因（{primary_dim}，按|变化量|降序）：{primary_summary}\n"
                        f"已下钻路径：\n{path_text}\n"
                        f"可下钻维值（parent={parent_dim}）："
                        f"{'、'.join(option_values) or '（无）'}\n"
                        f"候选维度：{candidates}\n"
                        "输出："
                    )
                ),
            ],
        )
    except Exception as exc:  # noqa: BLE001 - LLM 规划失败回落规则兜底
        logger.warning("归因下钻规划 LLM 失败，回落规则兜底：%s", exc)
        return None
    return {
        "action": step.action,
        "parent_dim": step.parent_dim,
        "value": step.value,
        "dim": step.dim,
        "reason": step.reason,
    }


async def _drilldown(
    state: AgentState,
    deps: AgentDeps,
    base_result: Dict[str, Any],
    dsl: Dict[str, Any],
    hstate: HarnessState,
    hctx: HarnessContext,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """递归下钻（P2，方案 §6.2）：对贡献 TOP 维值用新维度再切一刀。

    编排（模型驱动优先，对齐 FR14）：
    - 每层先问 LLM（diagnosis_plan）：drill / stop；返回 stop 或非法动作
      （dim 不在候选、value 不在已知维值、parent 链断裂）回落规则兜底；
    - 规则兜底只在首层动作（贡献最大维值 + 声明顺序候选维度），此后停止；
    - 每层一次 `diagnose_fetch` 补取数：DSL 过滤到父维值 + 时间维度 + 新维度
      （剥离排序/TopN/二次分析），预算计数入 hstate；
    - 任一层取数失败/无有效数据：记录 warning 并停止（部分路径仍然可用）。

    触发门槛：异动判定 significant（轻度波动不值得额外取数深挖）。
    返回 (drilldown 路径, warnings)。
    """
    ds = deps.diagnosis_settings
    warnings: List[str] = []
    if not ds.enabled or not ds.drilldown_enabled or ds.max_drill_depth <= 0:
        return [], []
    if not base_result.get("ok"):
        return [], []
    if (base_result.get("anomaly") or {}).get("status") != "significant":
        return [], []
    primary_dim = base_result.get("primary_dim")
    metric = base_result.get("metric")
    layer = deps.semantic_layer
    engine = deps.metric_engine
    if not primary_dim or not metric or layer is None or engine is None:
        return [], []

    time_dim = layer.time_dimension_name()
    if not time_dim:
        return [], []
    metric_obj = layer.resolve_metric(metric)
    if metric_obj is None:
        return [], []

    # 候选下钻维度 = 指标可用维度 - 时间维度 - 结果已有维度（下钻要引入新维度才有信息增量；
    # 已在结果里的维度组合走 cross_attribution，无需取数）
    used = [d for d in (dsl.get("dimensions") or []) if d != time_dim]
    candidates = [
        d for d in layer.supported_dimensions(metric_obj)
        if d != time_dim and d not in used
    ]
    primary_list = (base_result.get("dimensions") or {}).get(primary_dim) or []
    parent_options = [r for r in primary_list if r.get("presence", "kept") == "kept"]
    if not candidates or not parent_options:
        return [], []

    llm = deps.get_chat_model() if ds.plan_model_driven else None
    harness = getattr(deps, "harness", None)
    use_period = base_result.get("compare_mode") == "period"

    path: List[Dict[str, Any]] = []
    drill_dims_used: List[str] = []
    filters = list(dsl.get("filters") or [])
    parent_dim = primary_dim

    while len(path) < ds.max_drill_depth and candidates and parent_options:
        step: Optional[Dict[str, Any]] = None
        if llm is not None:
            step = await _plan_next_drill(
                llm, state, base_result, path, parent_dim, parent_options, candidates
            )
        if step is not None and step.get("action") == "stop":
            break
        # 合法性校验：LLM 只能在候选维度 / 已知维值 / 正确父链内决策
        valid = (
            step is not None
            and step.get("dim") in candidates
            and str(step.get("parent_dim")) == str(parent_dim)
            and any(str(o.get("dim_value")) == str(step.get("value")) for o in parent_options)
        )
        if not valid:
            step = _rule_drill_step(parent_options, candidates, path)
            if step is None:
                break

        drill_filter = {
            "dimension": str(step["parent_dim"]),
            "dimension_value": [step["value"]],
            "filter_operator": "in",
        }
        fetch_dsl = _attribution_fetch_dsl(
            dsl, [time_dim, step["dim"]], extra_filters=filters + [drill_filter]
        )

        async def _raw():
            return await engine.query(fetch_dsl)

        try:
            if harness is not None:
                fetched = await harness.invoke(
                    "diagnose_fetch", {"dsl": fetch_dsl}, hctx, hstate, _raw
                )
            else:
                fetched = await _raw()
        except Exception as exc:  # noqa: BLE001 - 下钻取数失败：保留已完成路径并停止
            logger.warning(
                "归因下钻取数失败（%s=%s × %s）：%s",
                step["parent_dim"], step["value"], step["dim"], exc,
            )
            warnings.append(
                f"下钻取数失败（{step['parent_dim']}={step['value']} × {step['dim']}），"
                f"已停止继续下钻，以上为已完成的归因路径。"
            )
            break

        fetched_rows = rows_to_dicts(fetched)
        fetched_columns = fetched.get("columns") or []
        time_col, has_time = _detect_time_column(fetched_columns, fetched_rows)
        if not fetched.get("rows") or not has_time:
            warnings.append(
                f"下钻取数未返回有效数据（{step['parent_dim']}={step['value']} × "
                f"{step['dim']}），已停止继续下钻。"
            )
            break

        if use_period:
            children = period_dim_attribution(fetched_rows, time_col, step["dim"], metric)
        else:
            children = dim_attribution(fetched_rows, time_col, step["dim"], metric)
        if not children:
            break

        parent_change = next(
            (
                o.get("change_value")
                for o in parent_options
                if str(o.get("dim_value")) == str(step["value"])
            ),
            None,
        )
        node: Dict[str, Any] = {
            "parent_dim": str(step["parent_dim"]),
            "parent_value": step["value"],
            "parent_change": parent_change,
            "dim": str(step["dim"]),
            "children": children[:_MAX_DIM_DETAIL],
        }
        if step.get("reason"):
            node["reason"] = str(step["reason"])
        path.append(node)
        drill_dims_used.append(str(step["dim"]))
        candidates = [d for d in candidates if d not in drill_dims_used]
        filters = filters + [drill_filter]
        # 下一层：父维度换成本次下钻维度，可下钻维值 = 本次 children 的 kept 段
        parent_dim = str(step["dim"])
        parent_options = [c for c in children if c.get("presence", "kept") == "kept"]

    return path, warnings


# ---------------- P2：行动建议接知识库 ----------------


async def _advice_docs(
    state: AgentState,
    deps: AgentDeps,
    base_result: Dict[str, Any],
    hstate: HarnessState,
    hctx: HarnessContext,
) -> List[Dict[str, Any]]:
    """行动建议知识召回（P2，方案 §6.4）：以「指标 + TOP 维值」检索口径/运营文档。

    走 harness 白名单工具 `retrieve_knowledge`（无 knowledge 权限会被拒——
    属预期降级）。无命中 / RAG 降级 / 检索器未装配一律返回空列表，
    保留既有模板 action_hints，不阻断主链路。
    """
    ds = deps.diagnosis_settings
    if not ds.enabled or not ds.advice_rag_enabled:
        return []
    retriever = deps.retriever
    if retriever is None or not base_result.get("ok"):
        return []
    same = base_result.get("same_contribution_list") or []
    opposite = base_result.get("opposite_contribution_list") or []
    terms = [str(base_result.get("metric") or "")]
    for item in list(opposite[:1]) + list(same[:1]):
        if item:
            terms.append(f"{item.get('dim_name')}={item.get('dim_value')}")
    query = " ".join(t for t in terms if t).strip()
    if not query:
        return []

    tenant_id = state.get("tenant_id") or None
    owner_id = state.get("user_id") or None

    async def _raw():
        try:
            return await retriever.retrieve(query, tenant_id=tenant_id, owner_id=owner_id)
        except TypeError:
            # 自定义检索器实现未跟上租户参数：降级为不隔离调用
            return await retriever.retrieve(query)

    harness = getattr(deps, "harness", None)
    try:
        if harness is not None:
            docs = await harness.invoke(
                "retrieve_knowledge", {"query": query}, hctx, hstate, _raw
            )
        else:
            docs = await _raw()
    except Exception as exc:  # noqa: BLE001 - 无权限/RAG 降级：保留模板建议，不阻断
        logger.info("归因行动建议知识召回未生效（保留模板建议）：%s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for d in (docs or [])[:3]:
        if isinstance(d, dict) and d.get("content"):
            out.append(
                {
                    "title": str(d.get("title") or d.get("source") or "")[:80],
                    "content": str(d.get("content") or "")[:400],
                    "source": str(d.get("source") or "")[:80],
                    "score": d.get("score"),
                }
            )
    return out


# ---------------- P3：因子归因 + 指标树归因 ----------------


def _attribution_period_totals(
    query_result: Dict[str, Any], metric: str
) -> Optional[Tuple[float, float]]:
    """从既有取数结果取某指标的（本期总量, 上期总量）——复用 P1 的对半分口径。"""
    columns = query_result.get("columns") or []
    rows = rows_to_dicts(query_result)
    if not rows:
        return None
    time_col, has_time = _detect_time_column(columns, rows)
    if not has_time:
        return None
    if metric not in columns:
        return None
    change = period_change(rows, time_col, metric)
    if change is None:
        return None
    return float(change.get("metrics_value") or 0.0), float(
        change.get("compare_metrics_value") or 0.0
    )


async def _factor_attribution(
    state: AgentState,
    deps: AgentDeps,
    base_result: Dict[str, Any],
    dsl: Dict[str, Any],
    hstate: HarnessState,
    hctx: HarnessContext,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """因子归因（P3 §7.1）：逐因子替换法，每个因子一次补取数。

    前提：语义层 metric 声明 factors 公式（expr + factors 映射）。因子值按
    因子自身口径取数（同指标/过滤/时间窗，无维度分组——因子是总量口径的拆解），
    本期/上期值由 P1 的期间对半分口径从因子取数结果计算。

    任一因子取数失败 / 因子值无效：整体跳过因子归因（返回 None）并在 warnings
    说明——部分因子缺失时替换法无法闭合恒等式，宁缺毋滥。
    """
    from dmf_agent.diagnosis.factors import factor_contribution, parse_factor_expr

    ds = deps.diagnosis_settings
    warnings: List[str] = []
    metric = base_result.get("metric")
    layer = deps.semantic_layer
    engine = deps.metric_engine
    if not ds.enabled or not ds.factors_enabled or not metric or layer is None or engine is None:
        return None, []
    metric_obj = layer.resolve_metric(metric)
    if metric_obj is None or not metric_obj.factors:
        return None, []

    factor_map = metric_obj.factors.get("factors") or {}
    expr = str(metric_obj.factors.get("expr") or "")
    ordered = parse_factor_expr(expr, factor_map)
    if not ordered:
        warnings.append(
            f"因子公式不可解析（expr={expr or '空'}），已跳过因子归因。"
        )
        return None, warnings

    # 逐因子取数：因子值 = 因子指标在归因口径（过滤/时间窗）下的期间总量
    baseline: Dict[str, float] = {}
    current: Dict[str, float] = {}
    for key in ordered:
        ref = str(factor_map[key])
        fetch_dsl = _attribution_fetch_dsl(dsl, [], )
        fetch_dsl["metrics"] = [{"name": ref}]
        async def _raw():
            return await engine.query(fetch_dsl)
        try:
            fetched = await _invoke_fetch(deps, state, hctx, hstate, fetch_dsl, _raw)
        except Exception as exc:  # noqa: BLE001 - 因子取数失败：整体跳过
            warnings.append(f"因子[{key}]（{ref}）取数失败（{exc}），已跳过因子归因。")
            return None, warnings
        totals = _attribution_period_totals(fetched, ref)
        if totals is None:
            warnings.append(f"因子[{key}]（{ref}）未返回有效期间数据，已跳过因子归因。")
            return None, warnings
        current[key], baseline[key] = totals  # (本期, 上期)

    result = factor_contribution(baseline, current, ordered)
    result["metric"] = metric
    result["expr"] = expr
    result["factor_refs"] = dict(factor_map)
    return result, warnings


async def _tree_attribution(
    state: AgentState,
    deps: AgentDeps,
    base_result: Dict[str, Any],
    dsl: Dict[str, Any],
    hstate: HarnessState,
    hctx: HarnessContext,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """指标树归因（P3 §7.2）：单层分解，各子指标一次补取数。

    前提：清单声明 metrics_tree 且含以当前指标为父节点的边。子指标按自身口径
    取数，期间变化量与根指标同口径（P1 对半分）；子指标取数失败仅该子项标记
    不可用（share 在可用子集内归一），不整体跳过——树分解容错性优于因子恒等式。
    """
    from dmf_agent.diagnosis.tree import tree_attribution

    ds = deps.diagnosis_settings
    warnings: List[str] = []
    metric = base_result.get("metric")
    layer = deps.semantic_layer
    engine = deps.metric_engine
    if not ds.enabled or not ds.tree_enabled or not metric or layer is None or engine is None:
        return None, []
    tree = layer.manifest.metrics_tree if layer is not None else []
    if not tree or not any(e.get("parent") == metric for e in tree):
        return None, []

    period_by_metric: Dict[str, Dict[str, Any]] = {}
    root_totals = _attribution_period_totals(
        state.get("query_result") or {}, metric
    )
    if root_totals is None:
        # 根指标总量可能要靠补取数结果（P1 补了时间轴），优先用 base_result.change
        change = base_result.get("change") or {}
        if change.get("metrics_value") is not None:
            root_totals = (
                float(change.get("metrics_value") or 0.0),
                float(change.get("compare_metrics_value") or 0.0),
            )
    if root_totals is None:
        warnings.append("根指标缺少期间总量数据，已跳过指标树归因。")
        return None, warnings
    period_by_metric[metric] = {
        "metrics_value": root_totals[0],
        "compare_metrics_value": root_totals[1],
        "change_value": root_totals[0] - root_totals[1],
    }

    for edge in [e for e in tree if e.get("parent") == metric]:
        child = str(edge.get("child") or "")
        fetch_dsl = _attribution_fetch_dsl(dsl, [])
        fetch_dsl["metrics"] = [{"name": child}]
        async def _raw():
            return await engine.query(fetch_dsl)
        try:
            fetched = await _invoke_fetch(deps, state, hctx, hstate, fetch_dsl, _raw)
            totals = _attribution_period_totals(fetched, child)
        except Exception as exc:  # noqa: BLE001 - 单子指标失败只标记不可用
            logger.warning("指标树子指标[%s]取数失败：%s", child, exc)
            warnings.append(f"树子指标[{child}]取数失败，该项未计入分解。")
            continue
        if totals is None:
            warnings.append(f"树子指标[{child}]未返回有效期间数据，该项未计入分解。")
            continue
        period_by_metric[child] = {
            "metrics_value": totals[0],
            "compare_metrics_value": totals[1],
            "change_value": totals[0] - totals[1],
        }

    result = tree_attribution(tree, metric, period_by_metric)
    return result, warnings


async def _invoke_fetch(
    deps: AgentDeps,
    state: AgentState,
    hctx: HarnessContext,
    hstate: HarnessState,
    fetch_dsl: Dict[str, Any],
    raw_fn,
) -> Dict[str, Any]:
    """归因补取数的统一入口：harness 在则走白名单，否则直连引擎。"""
    harness = getattr(deps, "harness", None)
    if harness is not None:
        return await harness.invoke("diagnose_fetch", {"dsl": fetch_dsl}, hctx, hstate, raw_fn)
    return await raw_fn()


# ---------------- 节点 ----------------


def build_diagnose_node(deps: AgentDeps):
    async def diagnose(state: AgentState) -> Dict[str, Any]:
        ds = deps.diagnosis_settings
        # harness 计数器贯穿 P1 补取数 / P2 下钻与建议召回，预算累计与 execute_query 同口径
        harness_state_raw = state.get("harness_state") or {}
        hstate = HarnessState(
            rounds=harness_state_raw.get("rounds", 0),
            tool_calls=harness_state_raw.get("tool_calls", 0),
            tokens_used=harness_state_raw.get("tokens_used", 0),
        )
        hctx = HarnessContext(
            session_id=state.get("session_id") or "",
            user_id=state.get("user_id") or "",
            tenant_id=state.get("tenant_id") or "",
            round_id=state.get("round_id") or "",
            user_permissions=list(state.get("user_permissions") or []),
        )
        initial_tool_calls = hstate.tool_calls

        # P1：自动补时间/维度取数（缺时间轴/无业务维度时）
        query_result = await _fetch_diagnosis_data(state, deps, hstate, hctx)
        dsl = dict(state.get("dsl") or {})
        # 补取数成功时，归因口径以**补取数的 DSL** 为准（维度列在补取数结果里，
        # 仍用原 DSL 会导致维度归因读不到列）；失败/未触发时透传原始 DSL。
        fetch_dsl = query_result.get("_diagnosis_fetch_dsl")
        if fetch_dsl:
            dsl["dimensions"] = list(fetch_dsl.get("dimensions") or [])
        # 期间/端点口径（P1）：settings 可整体关闭或显式 endpoint
        if ds.enabled and ds.compare_mode == "period":
            dsl.setdefault("compare_mode", "period")
        else:
            dsl["compare_mode"] = "endpoint"
        result = _diagnose(query_result, dsl)
        warnings = [w for w in (query_result.get("_diagnosis_warnings") or []) if w]

        # ---- P2：交叉归因（零取数）+ 递归下钻（补取数）+ 行动建议召回（均不阻断） ----
        if result.get("ok") and ds.enabled:
            cross = _compute_cross(query_result, dsl, ds, result)
            if cross:
                result["cross"] = cross
            drilldown, drill_warnings = await _drilldown(
                state, deps, result, dsl, hstate, hctx
            )
            if drilldown:
                result["drilldown"] = drilldown
            warnings.extend(drill_warnings)
            advice = await _advice_docs(state, deps, result, hstate, hctx)
            if advice:
                result["advice_docs"] = advice

            # ---- P3：因子归因（每个因子一次补取数）+ 指标树归因（每个子指标一次） ----
            if result.get("anomaly", {}).get("status") == "significant":
                factors, f_warnings = await _factor_attribution(
                    state, deps, result, dsl, hstate, hctx
                )
                if factors:
                    result["factors"] = factors
                warnings.extend(f_warnings)
                tree, t_warnings = await _tree_attribution(
                    state, deps, result, dsl, hstate, hctx
                )
                if tree:
                    result["tree"] = tree
                warnings.extend(t_warnings)

        if warnings:
            result["warnings"] = warnings
        if fetch_dsl:
            result["fetched_dsl"] = fetch_dsl
        logger.info("归因结果：%s", result)
        update: Dict[str, Any] = {"diagnosis": result}
        # 补取数/下钻/召回经 harness 消耗轮次/工具调用计数 -> 回写预算
        if hstate.tool_calls > initial_tool_calls:
            update["harness_state"] = hstate.snapshot()
        return update

    return diagnose
