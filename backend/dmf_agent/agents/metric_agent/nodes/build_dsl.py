"""查询计划节点：查询要素 -> 语义查询计划（MetricFlow 语义查询，DSL v3）。

与旧实现的区别（2026-09 主流程脱离指标平台）：
- 不再把业务指标名映射为指标平台编码（hm_metric_id / hm_dim_id）；
- 指标名在语义层内解析为标准名后原样保留，物理表/列由语义清单承载，
  由 semantic/compiler.py 编译为 StarRocks SQL；
- 维度名保留为业务维度名，过滤条件统一为
  {dimension, dimension_value, filter_operator}。

产出结构（写入 state.dsl，随 trace 落库供前端展示）：

    {
      "version": "3.0",
      "engine": "metricflow",
      "metrics": [{"name": "GMV"}],
      "dimensions": ["统计日期", "渠道"],
      "filters": [{"dimension": "渠道", "dimension_value": [...], "filter_operator": "in"}],
      "time_range": {"start": "...", "end": "...", "granularity": "day"},
      "order_by": "GMV desc",
      "limit": 10,
      "rank_scope": "global",                 # FR1：global | group（时间序列组内排名）
      "period_comparison": true,
      "period_comparison_type": "yoy",
      "period_comparison_types": ["yoy"],     # FR1：可并存，如 ["yoy", "mom"]
      "advanced": {...}   # 二次分析配置，由 analyze_data 追加
    }
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState
from dmf_agent.semantic.query import normalize_filter

logger = logging.getLogger(__name__)

DSL_VERSION = "3.0"


def build_build_dsl_node(deps: AgentDeps):
    async def build_dsl(state: AgentState) -> Dict[str, Any]:
        elements = state.get("query_elements") or {}
        layer = deps.semantic_layer

        metrics: List[Dict[str, str]] = []
        for name in elements.get("metrics") or []:
            metric = layer.resolve_metric(name) if layer is not None else None
            standard = metric.name if metric is not None else name
            if standard and {"name": standard} not in metrics:
                metrics.append({"name": standard})

        dsl: Dict[str, Any] = {
            "version": DSL_VERSION,
            "engine": "metricflow",
            "metrics": metrics,
            "dimensions": [d for d in (elements.get("dimensions") or []) if d],
            "filters": [normalize_filter(f) for f in elements.get("filters") or []],
            "metric_filters": _normalize_metric_filters(
                elements.get("metric_filters") or [], layer
            ),
            "time_range": elements.get("time_range"),
            "order_by": elements.get("order_by"),
            "limit": elements.get("limit"),
            # FR1 排名范围：global 全局排名 | group 时间序列组内排名
            "rank_scope": elements.get("rank_scope") or "global",
            "period_comparison": bool(elements.get("period_comparison", False)),
            "period_comparison_type": elements.get("period_comparison_type"),
            # FR1 同环比可并存（同比 + 环比）：多个平移窗口各自输出增长值/增长率
            "period_comparison_types": list(elements.get("period_comparison_types") or []),
        }
        logger.info("语义查询计划组装完成：%s", dsl)
        return {"dsl": dsl}

    return build_dsl


def _normalize_metric_filters(items: list, layer) -> List[Dict[str, Any]]:
    """指标阈值归一：指标名解析为语义层标准名，去重，非法值丢弃。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("metric") or item.get("metric_name")
        operator = str(item.get("operator") or ">").strip()
        if operator in ("==",):
            operator = "="
        if operator not in (">", "<", ">=", "<=", "=", "!="):
            continue
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        resolved = None
        if layer is not None and raw_name:
            metric = layer.resolve_metric(str(raw_name))
            resolved = metric.name if metric is not None else None
        name = resolved or (str(raw_name) if raw_name else "")
        if not name:
            continue
        key = (name, operator)
        if key in seen:
            continue
        seen.add(key)
        out.append({"metric": name, "operator": operator, "value": value})
    return out
