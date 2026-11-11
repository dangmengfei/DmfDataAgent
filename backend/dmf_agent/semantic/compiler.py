"""MetricFlow 规范 SQL 编译器：语义查询 -> StarRocks SQL。

编译策略（对齐 MetricFlow 的"measure 层聚合 + 语义层公式上提"思路）：

1. **展开**：把查询的每个指标递归展开到 measure 层（simple 自身；ratio / derived
   展开引用链），得到 `(measure, agg)` 集合与它们归属的语义模型。
2. **分模型聚合**：每个语义模型生成一个 CTE，在**分组键粒度**上聚合出所有
   measure 列（`SUM/AVG/COUNT(DISTINCT)/...`），并下推时间范围与维度过滤。
3. **对齐合并**：多模型时按分组键 `FULL OUTER JOIN`（跨数据域的指标/比率
   必须先对齐粒度再计算，与 MetricFlow 跨 semantic model 的行为一致）。
4. **公式上提**：ratio / derived 在 JOIN 之后的最外层计算（`分子/分母`、
   `表达式`），避免在 CTE 内重复聚合；占比 / 排名在此之上用窗口函数补列
   （组内排名 `rank_scope=group` 时加 `PARTITION BY <时间列>`，每期内部各自排名，
   且组内 TopN 走「名次列过滤」而非全局 LIMIT）。
5. **期间对比**：同环比不是"再查一次"，而是把整个 CTE 集合生成多份
   （当期 + 各对比期；对比期把时间窗与分组时间键整体平移 lag），再对齐输出
   `__前期 / __增长值 / __增长率`。FR1 支持**同比 + 环比并存**：多类型时
   对比期 CTE 为 `p / p2 / …`，列名带类型后缀（`M__增长率_mom`）；单类型
   保持历史列名不变（向后兼容）。

方言：StarRocks（MySQL 协议）——标识符用反引号（StarRocks 中双引号是字符串
字面量），时间粒度用 `date_trunc(unit, expr)`，月份平移用 `add_months`，
天数平移用 `DATE_ADD(expr, INTERVAL n DAY)`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from dmf_agent.config.settings import StarRocksSettings
from dmf_agent.semantic.dialects import get_dialect
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.manifest import (
    MEASURE_AGGS,
    SUPPORTED_AGGS,
    Measure,
    Metric,
    SemanticModel,
)
from dmf_agent.semantic.query import MetricQuery

# derived 表达式词法扫描允许的运算符 / 数字字符（其余字符必须是已登记的指标名）
_EXPR_SAFE_CHARS = set("+-*/(). \t")

_GRANULARITY_TO_TRUNC = {
    "day": "day",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}

# 允许二次分析覆盖聚合方式的 measure（预聚合列才有"日均/月均"等语义）
_OVERRIDABLE_AGGS = {"sum", "avg", "max", "min"}

# 同环比对比期 CTE 前缀（第一个固定为 "p"，与历史 SQL 完全一致；多类型时继续 p2/p3…）
_PERIOD_PREFIXES = ("p", "p2", "p3", "p4")


def _period_alias(metric: str, kind: str, pop_type: str, multi: bool) -> str:
    """同环比列名。

    单类型保持历史别名（`M__前期 / M__增长值 / M__增长率`，向后兼容既有消费方）；
    多类型（同比 + 环比并存）追加类型后缀（`M__增长率_mom`），避免列名冲突。
    """
    return f"{metric}__{kind}_{pop_type}" if multi else f"{metric}__{kind}"


class CompileError(ValueError):
    """语义查询无法编译（指标/维度不可用、缺时间范围、表达式非法等）。"""


@dataclass
class CompiledQuery:
    sql: str
    columns: List[str]
    group_columns: List[str] = field(default_factory=list)
    metric_columns: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    period_columns: List[str] = field(default_factory=list)


@dataclass
class _GroupKey:
    """分组键：对外别名 + 各模型内的物理表达式（不含 date_trunc）。"""

    alias: str
    kind: str  # time | categorical
    dimension: str
    exprs: Dict[str, str] = field(default_factory=dict)  # model_name -> "col"


@dataclass
class _MeasureCol:
    measure: Measure
    agg: str
    model: SemanticModel
    # 两阶段聚合：该 measure 列需经"日内聚合 -> 目标粒度再聚合"。
    # two_stage_outer 为外层聚合（如 avg）；None 表示单阶段（原逻辑）。
    two_stage: bool = False
    two_stage_outer: Optional[str] = None

    @property
    def alias(self) -> str:
        return _measure_col_alias(
            self.measure.name, self.agg, self.two_stage, self.two_stage_outer
        )


def _measure_col_alias(
    measure_name: str, agg: str, two_stage: bool, two_stage_outer: Optional[str] = None
) -> str:
    """measure 聚合列在 CTE 中的别名。

    两阶段聚合用 ``__ts`` 后缀区分单阶段同列；当外层聚合非默认的 avg 时（如
    last/first/max/min）再追加 ``_<outer>`` 后缀，避免「同一 measure + 不同外层聚合」
    的两个指标在 CTE 中共享同一列别名而互相覆盖。avg 不追加（保持历史别名向后兼容）。
    """
    base = f"m_{measure_name}_{agg}"
    if two_stage:
        suffix = f"_{two_stage_outer}" if two_stage_outer and two_stage_outer != "avg" else ""
        base += "__ts" + suffix
    return base


def _literal(value: str) -> str:
    """字符串字面量（单引号，内部单引号与反斜杠转义）。"""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# ---------------------------------------------------------------- 时间平移


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _add_months(value: date, months: int) -> date:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(value.day, _days_in_month(year, month)))


def _shift(value: date, months: int = 0, days: int = 0) -> date:
    out = _add_months(value, months) if months else value
    return date.fromordinal(out.toordinal() + days) if days else out


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def lag_delta(pop_type: Optional[str], granularity: str) -> Dict[str, int]:
    """同环比类型的平移量（月 / 天）；pop 按当前时间粒度平移一个周期。"""
    kind = (pop_type or "pop").lower()
    if kind == "yoy":
        return {"months": 12}
    if kind == "mom":
        return {"months": 1}
    if kind == "qoq":
        return {"months": 3}
    if kind == "wow":
        return {"days": 7}
    return {
        "day": {"days": 1},
        "week": {"days": 7},
        "month": {"months": 1},
        "quarter": {"months": 3},
        "year": {"months": 12},
    }.get(granularity, {"days": 1})


def _negate(delta: Dict[str, int]) -> Dict[str, int]:
    return {k: -v for k, v in delta.items()}


# ---------------------------------------------------------------- 编译器


class MetricFlowSqlCompiler:
    def __init__(
        self,
        layer: SemanticLayer,
        settings: Optional[StarRocksSettings] = None,
    ) -> None:
        self.layer = layer
        self.settings = settings or StarRocksSettings()
        self.time_alias = self.settings.time_column_alias or "date"
        # SQL 方言（starrocks 默认；mysql/postgresql/oracle 见 semantic/dialects.py）
        self._dialect = get_dialect(self.settings.dialect)
        self.dialect = self._dialect.name
        self._q = self._dialect.ident

    # ---------------- 入口 ----------------

    def compile(
        self,
        query: MetricQuery,
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> CompiledQuery:
        metrics = self._resolve_metrics(query)
        if not metrics:
            raise CompileError("查询缺少指标，无法编译")

        models = self._involved_models(metrics)
        keys = self._build_group_keys(query, models)
        measure_cols = self._build_measure_cols(query, metrics)
        agg_by_metric: Dict[str, Dict[str, Any]] = {}
        for metric in metrics:
            mapping: Dict[str, Any] = {}
            for measure_name, agg, ts in self._metric_measures(metric, query):
                mapping[measure_name] = {
                    "agg": agg,
                    "ts": ts is not None,
                    "ts_outer": (ts["outer"] if ts else None),
                }
            agg_by_metric[metric.name] = mapping
        join_type = (self.settings.join_type or "full").lower()

        # FR1：同环比可并存（同比 + 环比）-> 每个类型一个对比期 CTE 组（p / p2 / …）
        pop_types = self._query_pop_types(query) if query.period_comparison else []
        prev_prefixes = list(_PERIOD_PREFIXES[: len(pop_types)])
        multi_pop = len(pop_types) > 1

        groups: List[Tuple[str, Optional[Dict[str, int]]]] = [("c", None)]
        if query.period_comparison:
            for idx, pop_type in enumerate(pop_types):
                delta, prev_window = self._period_window_for(pop_type, query)
                if prev_window is None:
                    raise CompileError("同环比查询需要明确的时间范围（start / end）")
                groups.append((prev_prefixes[idx], delta))

        ctes: List[str] = []
        joined_names: Dict[str, str] = {}
        for prefix, delta in groups:
            model_ctes, joined = self._build_group_ctes(
                prefix, delta, query, keys, measure_cols, models, join_type, row_scopes
            )
            ctes.extend(model_ctes)
            joined_names[prefix] = joined

        columns: List[str] = [k.alias for k in keys]
        select_items: List[str] = []
        for key in keys:
            if query.period_comparison:
                select_items.append(
                    "COALESCE("
                    + ", ".join(
                        f"{p}.{self._q(key.alias)}" for p in ("c", *prev_prefixes)
                    )
                    + f") AS {self._q(key.alias)}"
                )
            else:
                select_items.append(f"c.{self._q(key.alias)} AS {self._q(key.alias)}")

        period_cols: List[str] = []
        for metric in metrics:
            select_items.append(
                f"{self._metric_sql(metric, measure_cols, 'c', agg_by_metric)}"
                f" AS {self._q(metric.name)}"
            )
            columns.append(metric.name)
            if not query.period_comparison:
                continue
            for prefix, pop_type in zip(prev_prefixes, pop_types):
                prev_alias = _period_alias(metric.name, "前期", pop_type, multi_pop)
                select_items.append(
                    f"{self._metric_sql(metric, measure_cols, prefix, agg_by_metric)}"
                    f" AS {self._q(prev_alias)}"
                )
                columns.append(prev_alias)
                period_cols.append(prev_alias)

        ctes.append(
            "final AS (\n    SELECT\n      "
            + ",\n      ".join(select_items)
            + "\n    "
            + self._join_clause(joined_names, keys, join_type, prev_prefixes)
            + "\n  )"
        )

        # ---- calc 层：只能引用 final 的**列**（不能引用同层别名），
        #      期间增长、占比、排名都必须在 final 之上再算一层 ----
        source = "final"
        calc_items: List[str] = ["*"]
        calc_columns: List[str] = []
        for metric in metrics:
            op = query.metric_ops.get(metric.name)
            if query.period_comparison:
                for pop_type in pop_types:
                    value_alias = _period_alias(metric.name, "增长值", pop_type, multi_pop)
                    rate_alias = _period_alias(metric.name, "增长率", pop_type, multi_pop)
                    prev_alias = _period_alias(metric.name, "前期", pop_type, multi_pop)
                    calc_items.append(
                        f"ROUND({self._q(metric.name)} - {self._q(prev_alias)}, 4)"
                        f" AS {self._q(value_alias)}"
                    )
                    calc_items.append(
                        f"ROUND(({self._q(metric.name)} - {self._q(prev_alias)})"
                        f" / NULLIF({self._q(prev_alias)}, 0) * 100, 2) AS {self._q(rate_alias)}"
                    )
                    calc_columns.extend([value_alias, rate_alias])
                    period_cols.extend([value_alias, rate_alias])
            if op is None:
                continue
            if op.proportion:
                alias = metric.name + "__占比"
                calc_items.append(
                    f"ROUND({self._q(metric.name)} / NULLIF(SUM({self._q(metric.name)}) OVER (), 0)"
                    f" * 100, 2) AS {self._q(alias)}"
                )
                calc_columns.append(alias)
            if op.rank or op.rank_group:
                alias = metric.name + "__排名"
                # 组内排名（FR1）：按时间列 PARTITION，每期内部各自排名
                partition = ""
                if op.rank_group and query.time_grouped:
                    partition = f"PARTITION BY {self._q(self.time_alias)} "
                calc_items.append(
                    f"ROW_NUMBER() OVER ({partition}ORDER BY {self._q(metric.name)} "
                    f"{'ASC' if op.rank_order == 'asc' else 'DESC'}) AS {self._q(alias)}"
                )
                calc_columns.append(alias)
        if calc_columns:
            ctes.append(
                "calc AS (\n    SELECT\n      "
                + ",\n      ".join(calc_items)
                + "\n    FROM final\n  )"
            )
            source = "calc"
            columns.extend(calc_columns)

        # 组内排名 + TopN：截断必须落在**每组内部**（按名次列过滤），
        # 不能用全局 LIMIT —— 否则只剩第一个时间点的前 N 行。
        rank_scoped_limit = bool(query.limit) and any(
            op.rank_group for op in query.metric_ops.values()
        )
        sql = (
            "WITH\n  "
            + ",\n  ".join(ctes)
            + f"\nSELECT *\nFROM {source}"
            + self._metric_filter_sql(query, metrics)
            + self._order_by_sql(query, keys, metrics)
            + (
                f"\nLIMIT {int(query.limit)}"
                if query.limit and not rank_scoped_limit
                else ""
            )
        )
        return CompiledQuery(
            sql=sql,
            columns=columns,
            group_columns=[k.alias for k in keys],
            metric_columns=[m.name for m in metrics],
            models=[m.name for m in models],
            period_columns=period_cols,
        )

    # ---------------- 解析 ----------------

    def _resolve_metrics(self, query: MetricQuery) -> List[Metric]:
        out: List[Metric] = []
        for name in query.metrics:
            metric = self.layer.resolve_metric(name)
            if metric is None:
                raise CompileError(f"语义层未收录指标或别名：{name}")
            if metric not in out:
                out.append(metric)
        return out

    def _involved_models(self, metrics: Sequence[Metric]) -> List[SemanticModel]:
        models: List[SemanticModel] = []
        for metric in metrics:
            for model in self.layer.metric_models(metric):
                if model not in models:
                    models.append(model)
        if not models:
            raise CompileError("指标未关联任何语义模型，语义清单可能不完整")
        return models

    def _build_group_keys(
        self,
        query: MetricQuery,
        models: Sequence[SemanticModel],
    ) -> List[_GroupKey]:
        """分组键 -> 各模型内的物理列；任一涉及模型缺该维度即报错。"""
        keys: List[_GroupKey] = []
        granularity = query.granularity or "day"
        if granularity not in _GRANULARITY_TO_TRUNC:
            raise CompileError(f"不支持的时间粒度：{granularity}")
        for dim_name in query.group_by:
            is_time = dim_name == query.time_dimension
            key = _GroupKey(
                alias=self.time_alias if is_time else dim_name,
                kind="time" if is_time else "categorical",
                dimension=dim_name,
            )
            for model in models:
                dim = model.dimension(dim_name)
                if dim is None:
                    raise CompileError(
                        f"语义模型[{model.name}]不提供维度[{dim_name}]，其指标无法按该维度分组"
                    )
                key.exprs[model.name] = dim.expr
            keys.append(key)
        return keys

    def _build_measure_cols(
        self, query: MetricQuery, metrics: Sequence[Metric]
    ) -> List[_MeasureCol]:
        """(measure, agg, two_stage) 去重集合：指标展开 + 二次分析聚合覆盖 + 两阶段聚合。"""
        cols: List[_MeasureCol] = []
        seen: Set[Tuple[str, str, bool, Optional[str]]] = set()
        for metric in metrics:
            for measure_name, agg, ts in self._metric_measures(metric, query):
                ts_outer = (ts["outer"] if ts else None)
                key = (measure_name, agg, ts is not None, ts_outer)
                if key in seen:
                    continue
                seen.add(key)
                model = self.layer.manifest.measure_owner(measure_name)
                if model is None:
                    raise CompileError(f"measure[{measure_name}] 无归属语义模型")
                measure = model.measure(measure_name)
                if measure is None:
                    raise CompileError(f"语义模型[{model.name}]中没有 measure[{measure_name}]")
                cols.append(
                    _MeasureCol(
                        measure=measure,
                        agg=agg,
                        model=model,
                        two_stage=ts is not None,
                        two_stage_outer=ts_outer,
                    )
                )
        return cols

    def _metric_measures(
        self, metric: Metric, query: MetricQuery
    ) -> List[Tuple[str, str, Optional[Dict[str, str]]]]:
        """指标展开到 (measure, agg, two_stage)。

        two_stage 为 None 表示单阶段；非 None 时含 inner（日内聚合）与 outer（目标粒度再聚合）。
        两阶段仅作用于 simple 指标（ratio/derived 在 JOIN 后计算，无意义）。
        """
        op = query.metric_ops.get(metric.name)
        two_stage: Optional[Dict[str, str]] = None
        if metric.two_stage and metric.type == "simple":
            two_stage = {
                "inner": (metric.two_stage or {}).get("inner"),
                "outer": (metric.two_stage or {}).get("outer") or "avg",
            }
        out: List[Tuple[str, str, Optional[Dict[str, str]]]] = []
        for measure_name in self.layer.base_measures(metric.name):
            model = self.layer.manifest.measure_owner(measure_name)
            measure = model.measure(measure_name) if model else None
            if measure is None:
                raise CompileError(f"measure[{measure_name}] 定义缺失")
            if two_stage is not None:
                inner = two_stage["inner"] or measure.agg
                if inner not in SUPPORTED_AGGS:
                    raise CompileError(
                        f"指标[{metric.name}] two_stage.inner 聚合不支持：{inner}"
                    )
                agg = inner
            else:
                agg = measure.agg
                if (
                    op is not None
                    and op.agg_override
                    and op.agg_override in MEASURE_AGGS
                    and measure.agg in _OVERRIDABLE_AGGS
                    and not op.proportion
                ):
                    agg = op.agg_override
            out.append((measure_name, agg, two_stage))
        return out

    # ---------------- CTE 生成 ----------------

    def _measure_aggregation_sql(self, c: "_MeasureCol", model: "SemanticModel") -> str:
        """生成单个 measure 的聚合子句（`agg AS alias`）。

        常规聚合走 MEASURE_AGGS 模板；`last`/`first` 是**时间相对聚合**——
        取该模型时间维度上最值对应的度量值（期间末/初值，如"月末时点余额"），
        依赖模型的时间维度列（已在 validate 阶段保证存在）。
        """
        e = f"t.{c.measure.expr}"
        agg = c.agg
        if agg in ("last", "first"):
            td = model.time_dimension()
            if td is None:
                raise CompileError(
                    f"measure[{c.measure.name}] 使用 {agg} 聚合，但语义模型[{model.name}]未提供时间维度"
                )
            tcol = f"t.{td.expr}"
            return f"{self._dialect.arg_value(agg, e, tcol)} AS {self._q(c.alias)}"
        if agg not in MEASURE_AGGS:
            raise CompileError(f"不支持的聚合方式：{agg}")
        return f"{MEASURE_AGGS[agg].format(e=e)} AS {self._q(c.alias)}"



    def _period_window_for(
        self, pop_type: Optional[str], query: MetricQuery
    ) -> Tuple[Dict[str, int], Optional[Tuple[str, str]]]:
        """返回（平移量, 对比期时间窗 (start, end)）；无有效时间范围时窗口为 None。"""
        delta = lag_delta(pop_type, query.granularity)
        start, end = _parse_date(query.start), _parse_date(query.end)
        if start is None or end is None:
            return delta, None
        back = _negate(delta)
        return delta, (
            _shift(start, **back).isoformat(),
            _shift(end, **back).isoformat(),
        )

    def _query_pop_types(self, query: MetricQuery) -> List[str]:
        """同环比类型列表（去重、规范顺序）；未指明类型时回落到按粒度的通用 pop。"""
        raw: List[str] = list(query.period_comparison_types or [])
        if not raw and query.period_comparison_type:
            raw = [str(query.period_comparison_type)]
        if not raw:
            raw = ["pop"]
        out: List[str] = []
        for item in raw:
            text = str(item or "").strip().lower()
            if text and text not in out:
                out.append(text)
        return out

    def _window_for(
        self, delta: Optional[Dict[str, int]], query: MetricQuery
    ) -> Optional[Tuple[str, str]]:
        """某组（当期 / 对比期）的时间窗；delta 非空表示对比期。"""
        start, end = _parse_date(query.start), _parse_date(query.end)
        if start is None or end is None:
            return None
        if not delta:
            return start.isoformat(), end.isoformat()
        back = _negate(delta)
        return _shift(start, **back).isoformat(), _shift(end, **back).isoformat()

    def _build_group_ctes(
        self,
        prefix: str,
        delta: Optional[Dict[str, int]],
        query: MetricQuery,
        keys: Sequence[_GroupKey],
        measure_cols: Sequence[_MeasureCol],
        models: Sequence[SemanticModel],
        join_type: str,
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> Tuple[List[str], str]:
        window = self._window_for(delta, query)
        granularity = query.granularity or "day"
        ctes: List[str] = []
        names: List[str] = []

        for model in models:
            name = f"{prefix}_{model.name}"
            names.append(name)
            cols_for_model = [c for c in measure_cols if c.model.name == model.name]
            if any(c.two_stage for c in cols_for_model):
                ctes.append(
                    self._two_stage_model_cte(
                        name, prefix, delta, query, keys, cols_for_model,
                        model, window, granularity, row_scopes,
                    )
                )
            else:
                ctes.append(
                    self._single_model_cte(
                        name, prefix, delta, query, keys, cols_for_model,
                        model, window, granularity, row_scopes,
                    )
                )

        joined = f"{prefix}_joined"
        ctes.append(self._join_cte(joined, names, models, keys, measure_cols, join_type))
        return ctes, joined

    def _single_model_cte(
        self,
        name: str,
        prefix: str,
        delta: Optional[Dict[str, int]],
        query: MetricQuery,
        keys: Sequence[_GroupKey],
        cols: Sequence[_MeasureCol],
        model: SemanticModel,
        window: Optional[Tuple[str, str]],
        granularity: str,
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> str:
        """单层聚合 CTE（既有逻辑）：在分组键粒度上聚合所有 measure 列。"""
        group_items: List[str] = []
        group_exprs: List[str] = []
        for key in keys:
            col = key.exprs[model.name]
            base = f"t.{col}"
            if key.kind == "time":
                if delta:
                    base = self._dialect.shift_time(base, delta)
                expr = self._dialect.date_trunc(granularity, base)
            else:
                expr = base
            group_items.append(f"{expr} AS {self._q(key.alias)}")
            group_exprs.append(expr)

        measure_items = [self._measure_aggregation_sql(c, model) for c in cols]
        where_parts: List[str] = []
        if window is not None:
            time_dim = (
                model.dimension(query.time_dimension)
                if query.time_dimension
                else None
            ) or model.time_dimension()
            if time_dim is None:
                raise CompileError(f"语义模型[{model.name}]缺时间维度，无法应用时间范围")
            where_parts.append(
                f"t.{time_dim.expr} >= {_literal(window[0])}"
                f" AND t.{time_dim.expr} <= {_literal(window[1])}"
            )
        where_parts.extend(self._dim_filter_sql(model, query))

        # FR13 P6 行权限下推：把用户可见的维度值范围注入最内层 WHERE（最严约束）。
        # 维度在当前模型不存在则跳过（避免误拦）；row_scopes 为空 = 无约束（向后兼容）。
        if row_scopes:
            for _dim_name, _allowed in row_scopes.items():
                _dim = model.dimension(_dim_name)
                if _dim is None:
                    continue
                _vals = [v for v in (_allowed or []) if v != ""]
                if not _vals:
                    continue
                _rendered = ", ".join(_literal(v) for v in _vals)
                where_parts.append(f"t.{_dim.expr} IN ({_rendered})")

        cte = (
            f"{name} AS (\n    SELECT\n      "
            + ",\n      ".join(group_items + measure_items)
            + f"\n    FROM {self._dialect.table_ref(model.table)} AS t"
        )
        if where_parts:
            cte += "\n    WHERE " + "\n      AND ".join(where_parts)
        if group_exprs:
            cte += "\n    GROUP BY\n      " + ",\n      ".join(group_exprs)
        cte += "\n  )"
        return cte

    def _two_stage_model_cte(
        self,
        name: str,
        prefix: str,
        delta: Optional[Dict[str, int]],
        query: MetricQuery,
        keys: Sequence[_GroupKey],
        cols: Sequence[_MeasureCol],
        model: SemanticModel,
        window: Optional[Tuple[str, str]],
        granularity: str,
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> str:
        """两阶段聚合 CTE：先按天聚合（内层），再在目标粒度上聚合（外层）。

        用于"月日均存款余额"等语义——`agg=avg` 直接算只是底表行均值，必须把
        "每日聚合值"先算出来、再在更高粒度上求均值。当前仅支持 sum 度量重聚合
        （日内 SUM、目标粒度 SUM/AVG 等可安全重组合；last/first 等时间相对聚合
        无法两层嵌套，遇之直接报错）。
        """
        for c in cols:
            if c.agg != "sum":
                raise CompileError(
                    f"两阶段聚合要求语义模型[{model.name}]的度量均为 sum 聚合，"
                    f"但 measure[{c.measure.name}] 为 {c.agg}"
                )
        time_dim = (
            model.dimension(query.time_dimension)
            if query.time_dimension
            else None
        ) or model.time_dimension()
        if time_dim is None:
            raise CompileError(f"语义模型[{model.name}]缺时间维度，无法做两阶段聚合")

        # 日间子查询的"天"时间桶（含对比期整体平移）
        day_base = f"t.{time_dim.expr}"
        if delta:
            day_base = self._dialect.shift_time(day_base, delta)
        day_time = self._dialect.day_trunc(day_base)

        cat_items: List[str] = []
        cat_group_sub: List[str] = []
        outer_group_keys: List[str] = []
        outer_group_exprs: List[str] = []
        has_time = any(k.kind == "time" for k in keys)
        if has_time:
            outer_time = self._dialect.date_trunc(granularity, f"sub.{self._q('__day')}")
        else:
            outer_time = None
        for key in keys:
            if key.kind == "time":
                continue
            ce = f"t.{key.exprs[model.name]}"
            cat_items.append(f"{ce} AS {self._q(key.alias)}")
            cat_group_sub.append(ce)
            outer_group_keys.append(f"sub.{self._q(key.alias)} AS {self._q(key.alias)}")
            # GROUP BY 只能放纯表达式（MySQL 不接受 `expr AS alias` 形式；SELECT 才带 AS）
            outer_group_exprs.append(f"sub.{self._q(key.alias)}")

        # 内层：全部 sum（已校验），列别名加 __day 后缀
        inner_items = [
            f"SUM(t.{c.measure.expr}) AS {self._q(c.alias + '__day')}" for c in cols
        ]
        # 外层：两阶段 -> 外层 agg；单阶段 sum -> 重 sum（= 目标粒度总量）
        outer_items: List[str] = []
        for c in cols:
            src = f"sub.{self._q(c.alias + '__day')}"
            if c.two_stage:
                outer = (c.two_stage_outer or "avg").lower()
                # 时间相对聚合（last/first）：取目标粒度内「最后/最初一天」的日内聚合值
                # —— 即"月末时点值/期初值"。依赖日内子查询的 __day 时间桶作取值依据。
                if outer in ("last", "first"):
                    td = model.time_dimension()
                    if td is None:
                        raise CompileError(
                            f"两阶段 outer={outer} 需要语义模型"
                            f"[{model.name}] 提供时间维度（measure={c.measure.name}）"
                        )
                    tcol = f"sub.{self._q('__day')}"
                    outer_items.append(
                        f"{self._dialect.arg_value(outer, src, tcol)} AS {self._q(c.alias)}"
                    )
                else:
                    outer_items.append(
                        f"{(c.two_stage_outer or 'avg').upper()}({src}) AS {self._q(c.alias)}"
                    )
            else:
                outer_items.append(f"{MEASURE_AGGS[c.agg].format(e=src)} AS {self._q(c.alias)}")

        # 内层 WHERE（对 t）
        where_parts: List[str] = []
        if window is not None:
            where_parts.append(
                f"t.{time_dim.expr} >= {_literal(window[0])}"
                f" AND t.{time_dim.expr} <= {_literal(window[1])}"
            )
        where_parts.extend(self._dim_filter_sql(model, query))
        if row_scopes:
            for _dim_name, _allowed in row_scopes.items():
                _dim = model.dimension(_dim_name)
                if _dim is None:
                    continue
                _vals = [v for v in (_allowed or []) if v != ""]
                if not _vals:
                    continue
                _rendered = ", ".join(_literal(v) for v in _vals)
                where_parts.append(f"t.{_dim.expr} IN ({_rendered})")

        day_select = ",\n      ".join(
            [day_time + " AS " + self._q("__day"), *cat_items, *inner_items]
        )
        day_cte = f"  SELECT\n      {day_select}\n  FROM {self._dialect.table_ref(model.table)} AS t"
        if where_parts:
            day_cte += "\n  WHERE " + "\n    AND ".join(where_parts)
        day_cte += "\n  GROUP BY\n      " + ",\n      ".join([day_time, *cat_group_sub])

        outer_select = []
        outer_group: List[str] = []
        if outer_time is not None:
            outer_select.append(f"{outer_time} AS {self._q(self.time_alias)}")
            outer_group.append(outer_time)
        outer_select.extend(outer_group_keys)
        outer_select.extend(outer_items)
        outer_group.extend(outer_group_exprs)

        sql = (
            f"{name} AS (\n"
            f"  SELECT\n      " + ",\n      ".join(outer_select) + "\n"
            f"  FROM (\n{day_cte}\n  ) AS sub"
        )
        if outer_group:
            sql += "\n  GROUP BY\n      " + ",\n      ".join(outer_group)
        sql += "\n  )"
        return sql

    def _join_cte(
        self,
        joined: str,
        names: Sequence[str],
        models: Sequence[SemanticModel],
        keys: Sequence[_GroupKey],
        measure_cols: Sequence[_MeasureCol],
        join_type: str,
    ) -> str:
        """多模型按分组键对齐；单模型直接透传（保持 CTE 命名统一）。"""
        if len(names) == 1:
            return f"{joined} AS (\n    SELECT * FROM {names[0]}\n  )"

        full = join_type != "left"
        # 方言不支持 FULL OUTER JOIN（如 MySQL）时强制降级为 LEFT JOIN
        # （缺一侧值的行在 ratio 中表现为 NULL）
        if full and not self._dialect.supports_full_outer_join:
            full = False
        select_items: List[str] = []
        for key in keys:
            if full:
                select_items.append(
                    "COALESCE("
                    + ", ".join(f"{chr(ord('a') + i)}.{self._q(key.alias)}" for i in range(len(names)))
                    + f") AS {self._q(key.alias)}"
                )
            else:
                select_items.append(f"a.{self._q(key.alias)} AS {self._q(key.alias)}")
        for i, model in enumerate(models):
            alias = chr(ord("a") + i)
            for col in measure_cols:
                if col.model.name == model.name:
                    select_items.append(f"{alias}.{self._q(col.alias)}")
        if not select_items:
            select_items = ["1"]

        sql = f"{joined} AS (\n    SELECT\n      " + ",\n      ".join(select_items)
        sql += f"\n    FROM {names[0]} AS a"
        for i in range(1, len(names)):
            alias = chr(ord("a") + i)
            on = " AND ".join(
                f"a.{self._q(k.alias)} = {alias}.{self._q(k.alias)}" for k in keys
            ) or "1 = 1"
            join_kw = "LEFT JOIN" if not full else "FULL OUTER JOIN"
            sql += f"\n    {join_kw} {names[i]} AS {alias} ON {on}"
        return sql + "\n  )"

    def _join_clause(
        self,
        joined_names: Dict[str, str],
        keys: Sequence[_GroupKey],
        join_type: str,
        prev_prefixes: Sequence[str],
    ) -> str:
        """当期组 LEFT/FULL JOIN 各对比期组（同比 + 环比并存时逐个 join）。"""
        if not prev_prefixes:
            return f"FROM {joined_names['c']} AS c"
        # 方言不支持 FULL OUTER JOIN（如 MySQL）时降级为 LEFT JOIN（同环比 JOIN）
        effective_left = (join_type == "left") or not self._dialect.supports_full_outer_join
        join_kw = "LEFT JOIN" if effective_left else "FULL OUTER JOIN"
        sql = f"FROM {joined_names['c']} AS c"
        for prefix in prev_prefixes:
            on = " AND ".join(
                f"c.{self._q(k.alias)} = {prefix}.{self._q(k.alias)}" for k in keys
            ) or "1 = 1"
            sql += f"\n    {join_kw} {joined_names[prefix]} AS {prefix} ON {on}"
        return sql

    def _dim_filter_sql(self, model: SemanticModel, query: MetricQuery) -> List[str]:
        """维度过滤下推到各模型 CTE；模型缺该维度则报错（避免静默漏过滤）。"""
        parts: List[str] = []
        for f in query.filters:
            if not f.dimension:
                continue
            dim = model.dimension(f.dimension)
            if dim is None:
                raise CompileError(
                    f"过滤条件引用了维度[{f.dimension}]，但语义模型[{model.name}]不提供该维度"
                )
            expr = f"t.{dim.expr}"
            op = (f.operator or "in").lower()
            values = [v for v in f.values if v != ""]
            if not values:
                continue
            if op in ("in", "not in"):
                rendered = ", ".join(_literal(v) for v in values)
                parts.append(f"{expr} {'NOT IN' if op == 'not in' else 'IN'} ({rendered})")
            elif op == "like":
                parts.append(
                    "("
                    + " OR ".join(f"{expr} LIKE {_literal('%' + v + '%')}" for v in values)
                    + ")"
                )
            else:
                sql_op = {"==": "=", "<>": "!="}.get(op, op)
                if len(values) > 1 and sql_op == "=":
                    parts.append(f"{expr} IN ({', '.join(_literal(v) for v in values)})")
                else:
                    parts.append(f"{expr} {sql_op} {_literal(values[0])}")
        return parts

    # ---------------- 辅助查询 ----------------

    def compile_dimension_values(self, dim_name: str, limit: int = 20) -> str:
        """维值候选查询：`SELECT DISTINCT <维度列>`（取首个提供该维度的语义模型）。

        用于过滤条件的候选值召回（配置开关 DMF_STARROCKS-ENABLE_DIM_VALUE_RECALL）。
        """
        models = self.layer.manifest.dimension_owners(dim_name)
        if not models:
            raise CompileError(f"维度[{dim_name}]未收录在语义层")
        model = models[0]
        dim = model.dimension(dim_name)
        if dim is None:  # pragma: no cover - dimension_owners 已保证
            raise CompileError(f"语义模型[{model.name}]不提供维度[{dim_name}]")
        expr = dim.expr
        return (
            f"SELECT DISTINCT {expr} AS {self._q('dim_value')}\n"
            f"FROM {self._dialect.table_ref(model.table)}\n"
            f"WHERE {expr} IS NOT NULL\n"
            f"ORDER BY {expr}\n"
            f"LIMIT {int(limit)}"
        )

    # ---------------- 公式 ----------------

    def _measure_sql(self, alias: str, prefix: str) -> str:
        return f"{prefix}.{self._q(alias)}"

    def _metric_sql(
        self,
        metric: Metric,
        measure_cols: Sequence[_MeasureCol],
        prefix: str,
        agg_by_metric: Dict[str, Dict[str, Any]],
        stack: Optional[List[str]] = None,
    ) -> str:
        """指标 -> 最终输出表达式（带 scale 换算与 ROUND）。"""
        body = self._metric_expr(metric, measure_cols, prefix, agg_by_metric, stack)
        if abs(metric.scale - 1.0) > 1e-9:
            body = f"({body}) * {metric.scale:g}"
        return f"ROUND({body}, {int(metric.round)})" if metric.round >= 0 else body

    def _metric_expr(
        self,
        metric: Metric,
        measure_cols: Sequence[_MeasureCol],
        prefix: str,
        agg_by_metric: Dict[str, Dict[str, Any]],
        stack: Optional[List[str]] = None,
    ) -> str:
        """指标 -> 未取整的 SQL 表达式（被其它指标引用时使用，避免中途取整丢精度）。

        agg_by_metric 记录每个指标在本轮查询里对每个 measure 采用的聚合方式
        （含二次分析的聚合覆盖、两阶段聚合标记），保证"CTE 里生成的列"与
        "公式引用的列"完全一致。
        """
        stack = list(stack or [])
        if metric.name in stack:
            raise CompileError(f"指标引用存在环：{' -> '.join(stack + [metric.name])}")
        stack.append(metric.name)

        def ref(name: str) -> str:
            ref_metric = self.layer.manifest.metric(name)
            if ref_metric is not None:
                return self._metric_expr(ref_metric, measure_cols, prefix, agg_by_metric, stack)
            info = agg_by_metric.get(metric.name, {}).get(name)
            if info is not None:
                agg, ts, ts_outer = info["agg"], info["ts"], info.get("ts_outer")
            else:
                agg, ts, ts_outer = self._default_agg(name), False, None
            return self._measure_sql(
                _measure_col_alias(name, agg, ts, ts_outer), prefix
            )

        if metric.type == "simple":
            measure = metric.measure
            if not measure:
                raise CompileError(f"指标[{metric.name}] 缺少 measure 定义")
            return ref(measure)
        if metric.type == "ratio":
            if not metric.numerator or not metric.denominator:
                raise CompileError(f"指标[{metric.name}] 缺少分子/分母定义")
            return f"({ref(metric.numerator)}) / NULLIF(({ref(metric.denominator)}), 0)"
        raw = str(metric.type_params.get("expr") or "")
        if not raw:
            raise CompileError(f"指标[{metric.name}] 缺少表达式定义")
        return self._render_expression(raw, metric, ref)

    def _default_agg(self, measure_name: str) -> str:
        model = self.layer.manifest.measure_owner(measure_name)
        measure = model.measure(measure_name) if model else None
        return measure.agg if measure else "sum"

    def _render_expression(self, raw: str, metric: Metric, ref) -> str:
        """安全渲染 derived 表达式：逐字符扫描，只允许 已登记指标名 / 数字 / 运算符。

        非白名单字符一律拒绝编译——语义清单可能来自外部导入，必须防注入。
        """
        names = sorted(metric.input_metrics, key=len, reverse=True)
        out: List[str] = []
        i = 0
        while i < len(raw):
            matched = next((n for n in names if n and raw.startswith(n, i)), None)
            if matched is not None:
                out.append(f"({ref(matched)})")
                i += len(matched)
                continue
            char = raw[i]
            if char.isdigit() or char in _EXPR_SAFE_CHARS:
                out.append(char)
                i += 1
                continue
            raise CompileError(
                f"指标[{metric.name}] 表达式含非法字符 {char!r}，已拒绝编译：{raw!r}"
            )
        return "".join(out)

    # ---------------- 排序 / 过滤 ----------------

    def _order_by_sql(
        self, query: MetricQuery, keys: Sequence[_GroupKey], metrics: Sequence[Metric]
    ) -> str:
        # 组内排名（FR1）：时间正序在前 + 名次升序，便于按时间轴阅读每期榜单
        if query.rank_scope == "group":
            items: List[str] = []
            for key in keys:
                if key.kind == "time":
                    items.append(f"{self._q(key.alias)} DESC")
                    break
            for metric in metrics:
                op = query.metric_ops.get(metric.name)
                if op is not None and op.rank_group:
                    items.append(f"{self._q(metric.name + '__排名')} ASC")
            if items:
                return "\nORDER BY " + ", ".join(items)
        raw = (query.order_by or "").strip()
        if raw:
            parts = raw.replace(",", " ").split()
            col = parts[0]
            direction = "ASC" if len(parts) > 1 and parts[1].lower().startswith("asc") else "DESC"
            targets = {k.alias for k in keys} | {m.name for m in metrics}
            if col in targets:
                return f"\nORDER BY {self._q(col)} {direction}"
        items: List[str] = []
        for key in keys:
            if key.kind == "time":
                items.append(f"{self._q(key.alias)} DESC")
                break
        if metrics:
            items.append(f"{self._q(metrics[0].name)} DESC")
        return ("\nORDER BY " + ", ".join(items)) if items else ""

    def _metric_filter_sql(self, query: MetricQuery, metrics: Sequence[Metric]) -> str:
        parts: List[str] = []
        for metric in metrics:
            op = query.metric_ops.get(metric.name)
            if op is None:
                continue
            # 组内排名 + TopN：按名次列截断到「每组前 N」，替代全局 LIMIT
            if op.rank_group and query.limit:
                parts.append(f"{self._q(metric.name + '__排名')} <= {int(query.limit)}")
            if not op.filter_operator or op.filter_value is None:
                continue
            sql_op = {"==": "=", "<>": "!="}.get(op.filter_operator, op.filter_operator)
            if sql_op.lower() not in ("=", "!=", ">", "<", ">=", "<="):
                continue
            parts.append(f"{self._q(metric.name)} {sql_op} {op.filter_value:g}")
        return ("\nWHERE " + " AND ".join(parts)) if parts else ""
