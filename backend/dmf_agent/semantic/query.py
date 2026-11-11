"""语义查询中间表示（MetricQuery）—— Agent 与 SQL 编译器的契约。

来源：build_dsl 节点把抽取出的要素（指标/维度/过滤/时间/排序/同环比）组装为
本结构，交给 semantic/compiler.py 编译为 StarRocks SQL。
与 MetricFlow 的 MetricFlowQuery 对应：metrics + group_by_items + where + time_range
+ order/limit + 期间对比（period-over-period）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.semantic.layer import SemanticLayer

# 过滤操作符归一（LLM 的 equal/not_in 等写法 -> SQL 运算符）
_OPERATOR_ALIASES = {
    "=": "=",
    "==": "=",
    "equal": "=",
    "!=": "!=",
    "<>": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "in": "in",
    "not_in": "not in",
    "not in": "not in",
    "like": "like",
    "contains": "like",
}


def normalize_filter(item: dict) -> dict:
    """任意形态过滤条件 -> 统一 KV 形式 {dimension, dimension_value, filter_operator}。

    兼容 LLM 的 field/operator/values 与候选召回产出的 KV 键。
    """
    dim = item.get("field") or item.get("dimension")
    values = item.get("values")
    if values is None:
        values = item.get("dimension_value")
    if values is None:
        values = []
    if not isinstance(values, (list, tuple)):
        values = [values]
    operator = _OPERATOR_ALIASES.get(str(item.get("operator") or "in").strip().lower(), "in")
    return {
        "dimension": dim,
        "dimension_value": [str(v) for v in values],
        "filter_operator": operator,
    }


@dataclass
class QueryFilter:
    dimension: str
    operator: str = "in"
    values: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "QueryFilter":
        raw = normalize_filter(item)
        return cls(
            dimension=raw["dimension"] or "",
            operator=raw["filter_operator"],
            values=raw["dimension_value"],
        )


@dataclass
class MetricOp:
    """指标级算子（对应二次分析：聚合覆盖 / 占比 / 排名 / 指标过滤 / top-n）。"""

    agg_override: Optional[str] = None
    proportion: bool = False
    rank: bool = False
    rank_order: str = "desc"
    rank_limit: Optional[int] = None
    # FR1 组内排名：按时间列 PARTITION 的窗口名次（每期内部各自排名）
    rank_group: bool = False
    filter_operator: str = ""
    filter_value: Optional[float] = None


@dataclass
class MetricQuery:
    metrics: List[str] = field(default_factory=list)
    group_by: List[str] = field(default_factory=list)
    filters: List[QueryFilter] = field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    granularity: str = "day"
    period_comparison: bool = False
    period_comparison_type: Optional[str] = None
    # FR1：同环比可**并存**（同比 + 环比）-> 多个平移窗口，各自输出增长值/增长率。
    # 空列表时回落到 period_comparison_type 单值（向后兼容）。
    period_comparison_types: List[str] = field(default_factory=list)
    order_by: Optional[str] = None
    limit: Optional[int] = None
    # FR1 排名范围：global 全局排名（默认）| group 时间序列组内排名
    rank_scope: str = "global"
    metric_ops: Dict[str, MetricOp] = field(default_factory=dict)
    # 时间维度名（语义层全局时间维度，默认 统计日期）
    time_dimension: Optional[str] = None

    # ---------------- 构造 ----------------

    @classmethod
    def from_dsl(cls, dsl: Dict[str, Any], layer: SemanticLayer) -> "MetricQuery":
        """由 DSL / 语义查询请求构造（`dimensions` 与 `group_by` 两种键名都接受）。"""
        metrics: List[str] = []
        for item in dsl.get("metrics") or []:
            name = item.get("name") if isinstance(item, dict) else str(item)
            if not name:
                continue
            metric = layer.resolve_metric(name)
            metrics.append(metric.name if metric is not None else name)

        time_range = dsl.get("time_range") or {}
        group_by = dsl.get("dimensions") or dsl.get("group_by") or []
        advanced = dsl.get("advanced") or {}
        pop_types = _resolve_pop_types(dsl)
        rank_scope = _resolve_rank_scope(dsl, advanced)
        query = cls(
            metrics=metrics,
            group_by=[str(d) for d in group_by],
            filters=[
                QueryFilter.from_dict(f)
                for f in dsl.get("filters") or []
                if (f.get("dimension") or f.get("field"))
            ],
            start=time_range.get("start"),
            end=time_range.get("end"),
            granularity=(time_range.get("granularity") or "day"),
            period_comparison=bool(dsl.get("period_comparison")) or bool(pop_types),
            period_comparison_type=pop_types[0] if pop_types else None,
            period_comparison_types=pop_types,
            order_by=dsl.get("order_by"),
            rank_scope=rank_scope,
            time_dimension=layer.time_dimension_name(),
        )
        # 限制条数优先级：显式 dsl.limit（六要素 TopN）> order_by 后缀 "limit N"（兼容旧路径）
        query.limit = _coerce_limit(dsl.get("limit")) or _extract_limit(dsl.get("order_by"))
        query.metric_ops = _build_metric_ops(advanced, metrics, rank_scope)
        # 指标阈值过滤（FR1：GMV>100万）合并进 metric_ops，编译为外层 WHERE（HAVING 语义）
        for mf in dsl.get("metric_filters") or []:
            if not isinstance(mf, dict):
                continue
            name = mf.get("metric") or mf.get("metric_name")
            metric = layer.resolve_metric(str(name)) if name else None
            resolved = metric.name if metric is not None else (str(name) if name else "")
            operator = str(mf.get("operator") or ">").strip()
            if operator == "==":
                operator = "="
            try:
                value = float(mf.get("value"))
            except (TypeError, ValueError):
                continue
            if not resolved or operator not in (">", "<", ">=", "<=", "=", "!="):
                continue
            op = query.metric_ops.setdefault(resolved, MetricOp())
            # advanced 未声明阈值时填充；同一指标以首个阈值为准
            if not op.filter_operator or op.filter_value is None:
                op.filter_operator = operator
                op.filter_value = value
        # 排名/占比触发的 top-n 落到查询级 limit
        for op in query.metric_ops.values():
            if op.rank_limit is not None:
                query.limit = op.rank_limit if query.limit is None else min(query.limit, op.rank_limit)
        return query

    # ---------------- 输出 ----------------

    @property
    def time_grouped(self) -> bool:
        return bool(self.time_dimension) and self.time_dimension in self.group_by

    @property
    def categorical_group_by(self) -> List[str]:
        return [d for d in self.group_by if d != self.time_dimension]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metrics": self.metrics,
            "group_by": self.group_by,
            "filters": [
                {"field": f.dimension, "operator": f.operator, "values": f.values}
                for f in self.filters
            ],
            "time_range": {
                "start": self.start,
                "end": self.end,
                "granularity": self.granularity,
            },
            "period_comparison": self.period_comparison,
            "period_comparison_type": self.period_comparison_type,
            "period_comparison_types": list(self.period_comparison_types),
            "order_by": self.order_by,
            "limit": self.limit,
            "rank_scope": self.rank_scope,
            "metric_ops": {
                k: {
                    "agg": v.agg_override,
                    "proportion": v.proportion,
                    "rank": v.rank,
                    "rank_order": v.rank_order,
                    "rank_group": v.rank_group,
                    "filter_operator": v.filter_operator,
                    "filter_value": v.filter_value,
                }
                for k, v in self.metric_ops.items()
            },
        }


# 同环比类型的规范顺序（并列时列名/展示顺序稳定）
_POP_TYPES: Tuple[str, ...] = ("yoy", "mom", "qoq", "wow", "pop")


def _resolve_pop_types(dsl: Dict[str, Any]) -> List[str]:
    """同环比类型列表：`period_comparison_types` 优先，回落单值 `period_comparison_type`。

    仅在 `period_comparison=true` 或已给出类型列表时生效（避免把空 dsl 判成同环比）。
    """
    if not dsl.get("period_comparison") and not dsl.get("period_comparison_types"):
        return []
    raw: List[Any] = []
    types = dsl.get("period_comparison_types")
    if isinstance(types, (list, tuple)):
        raw.extend(types)
    elif types:
        raw.append(types)
    raw.append(dsl.get("period_comparison_type"))
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip().lower()
        if text in _POP_TYPES and text not in out:
            out.append(text)
    return sorted(out, key=_POP_TYPES.index)


def _resolve_rank_scope(dsl: Dict[str, Any], advanced: Dict[str, Any]) -> str:
    """排名范围：`dsl.rank_scope` 优先，回落 `advanced.ranking_method`（LLM/老通道）。"""
    for value in (dsl.get("rank_scope"), advanced.get("ranking_method")):
        text = str(value or "").strip().lower()
        if text == "group":
            return "group"
        if text == "global":
            return "global"
    return "global"


def _coerce_limit(value: Any) -> Optional[int]:
    """显式 limit（int 或纯数字字符串）→ 正整数；其余返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value) >= 1:
        return int(value)
    return None


def _extract_limit(order_by: Optional[str]) -> Optional[int]:
    """order_by 形如 "GMV desc limit 5" / "前5" 时解析 top-n。"""
    if not order_by:
        return None
    text = str(order_by).lower()
    if "limit" in text:
        tail = text.split("limit", 1)[1].strip().split()[0]
        if tail.isdigit():
            return int(tail)
    return None


def _build_metric_ops(
    advanced: Dict[str, Any], metrics: List[str], rank_scope: str = "global"
) -> Dict[str, MetricOp]:
    """二次分析配置 -> 指标算子（聚合覆盖 / 占比 / 排名 / 指标过滤 / top-n）。

    同名指标的多个配置项按字段**合并**（后者只补空值，不覆盖已有值）。

    历史行为是 `ops[name] = op` 直接覆盖：把 "agg=avg" 与 "proportion=true" 拆成
    两个 item 时，后者会整条替换前者，聚合覆盖被静默丢弃（前端/LLM 很容易这么生成）。
    合并后两类算子可共存。

    `rank_scope="group"`（FR1 时间序列组内排名）时，对**每个**指标都产出名次列——
    即使该指标在 advanced 里没有配置项，也要有 `__排名`，否则"各分行每月排名"会缺列。
    """
    ops: Dict[str, MetricOp] = {}
    for item in advanced.get("metric_result") or []:
        name = item.get("metric_name")
        if not name:
            continue
        op = ops.setdefault(name, MetricOp())
        op.agg_override = item.get("agg") or op.agg_override
        op.proportion = bool(item.get("proportion")) or op.proportion
        if item.get("orderby"):
            op.rank = True
            op.rank_order = str(item["orderby"]).lower()
        limit = item.get("limit")
        if str(limit or "").isdigit():
            limit_num = int(limit)
            op.rank_limit = (
                limit_num if op.rank_limit is None else min(op.rank_limit, limit_num)
            )
        if item.get("filter_operator"):
            op.filter_operator = str(item["filter_operator"])
        value = _to_float(item.get("condition_value"))
        if value is not None:
            op.filter_value = value
    if rank_scope == "group":
        for name in metrics:
            op = ops.setdefault(name, MetricOp())
            op.rank = True
            op.rank_group = True
    # 纯基础聚合（无任何算子）不产生算子，避免污染 SQL
    return {
        name: op
        for name, op in ops.items()
        if (
            (op.agg_override and op.agg_override != "sum")
            or op.proportion
            or op.rank
            or (op.filter_operator and op.filter_value is not None)
        )
    }


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
