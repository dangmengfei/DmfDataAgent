"""MetricFlow 语义引擎：语义查询 -> SQL -> StarRocks -> 结构化结果。

对外只暴露两件事：
- `compile(query|dsl)`：只编译不执行（联调 / 排查 / 前端"查看 SQL"）
- `query(query|dsl)`：编译 + 执行 + 结果规范化

引擎实现可替换（provider）：当前是内置的 MetricFlow 规范编译器
（semantic/compiler.py），未来若改用官方 dbt-metricflow / datus-metricflow，
只需实现同样的 compile/query 契约，语义清单与 Agent 侧无需改动。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union

from dmf_agent.config.settings import get_settings
from dmf_agent.semantic.compiler import CompiledQuery, MetricFlowSqlCompiler
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.query import MetricQuery
from dmf_agent.semantic.query_cache import get_query_cache
from dmf_agent.semantic.starrocks import StarRocksClient, StarRocksError

logger = logging.getLogger(__name__)

ENGINE_NAME = "metricflow"


@dataclass
class SemanticQueryResult:
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    sql: str = ""
    engine: str = ENGINE_NAME
    models: List[str] = field(default_factory=list)
    group_columns: List[str] = field(default_factory=list)
    metric_columns: List[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    mock: bool = False
    # P2-5 结果元信息：数据截至时间（各模型时间维度 MAX 的最新值）+ 口径水印
    data_as_of: Optional[str] = None
    caliber: Dict[str, Any] = field(default_factory=dict)
    # P2-1 查询结果缓存：本次结果是否来自缓存命中（观测/前端展示用）
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "sql": self.sql,
            "engine": self.engine,
            "models": self.models,
            "group_columns": self.group_columns,
            "metric_columns": self.metric_columns,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            # 主流程已无 mock 分支，字段保留用于观测/前端契约兼容
            "mock": self.mock,
            # P2-5 结果元信息（PRD 5.5 / 空数据回溯依赖）：口径水印 + 数据截至时间
            "data_as_of": self.data_as_of,
            "caliber": self.caliber,
            # P2-1 缓存命中标记
            "cached": self.cached,
        }


class MetricFlowEngine:
    def __init__(
        self,
        layer: SemanticLayer,
        executor: StarRocksClient,
        compiler: Optional[MetricFlowSqlCompiler] = None,
    ) -> None:
        self.layer = layer
        self.executor = executor
        self.compiler = compiler or MetricFlowSqlCompiler(layer, executor.settings)

    # ---------------- 编译 ----------------

    @staticmethod
    def to_query(source: Union[MetricQuery, Dict[str, Any]], layer: SemanticLayer) -> MetricQuery:
        if isinstance(source, MetricQuery):
            return source
        return MetricQuery.from_dsl(source, layer)

    def compile(
        self,
        source: Union[MetricQuery, Dict[str, Any]],
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> CompiledQuery:
        return self.compiler.compile(self.to_query(source, self.layer), row_scopes=row_scopes)

    # ---------------- 执行 ----------------

    async def query(
        self,
        source: Union[MetricQuery, Dict[str, Any]],
        row_scopes: Optional[Dict[str, Set[str]]] = None,
    ) -> Dict[str, Any]:
        """编译并执行；失败抛 StarRocksError / CompileError（不降级、不返回假数据）。

        `row_scopes`：行权限（FR13 P6 enforce），下推到 SQL 最内层 WHERE。
        P2-1：相同（语义层命名空间 + 查询参数 + 行权限范围）的查询命中内存缓存时直出，
        跳过编译后取数与元信息富集（都已在首次结果里固化）。缓存关闭/异常均静默放过。
        """
        # 先归一为 MetricQuery，供口径水印提取指标/维度（避免重复解析）+ 缓存 key 稳定
        mq = self.to_query(source, self.layer)

        # ---- P2-1：缓存查询 ----
        sem = get_settings().semantic
        cache = get_query_cache(ttl_sec=sem.query_cache_ttl_sec) if sem.query_cache_enabled else None
        namespace = str(self.layer.manifest.path)
        if cache is not None:
            cached_payload = cache.get(namespace, mq, row_scopes)
            if cached_payload is not None:
                logger.info("语义查询命中缓存（namespace=%s）", namespace)
                return cached_payload

        compiled = self.compiler.compile(mq, row_scopes=row_scopes)
        logger.info(
            "语义查询编译完成（engine=%s models=%s columns=%s）\nSQL:\n%s",
            ENGINE_NAME,
            compiled.models,
            compiled.columns,
            compiled.sql,
        )
        result = await self.executor.fetch(compiled.sql)
        payload = SemanticQueryResult(
            columns=result["columns"] or compiled.columns,
            rows=result["rows"],
            sql=compiled.sql,
            models=compiled.models,
            group_columns=compiled.group_columns,
            metric_columns=compiled.metric_columns,
            row_count=result["row_count"],
            truncated=result["truncated"],
            elapsed_ms=result["elapsed_ms"],
        )
        if payload.truncated:
            logger.warning(
                "查询结果被截断：row_count=%s（上限 %s）",
                payload.row_count,
                self.executor.settings.max_rows,
            )

        # P2-5 / FR12 结果元信息：数据截至时间 + 口径说明（失败静默降级，绝不阻断主链路）
        try:
            as_of = await self.data_as_of(compiled.models)
            payload.data_as_of = as_of
            payload.caliber = build_caliber(
                self.layer.manifest,
                mq.metrics,
                mq.categorical_group_by,
                compiled.models,
                data_as_of=as_of,
                query=mq,
                row_scopes=row_scopes,
            )
        except Exception:  # noqa: BLE001 - 元信息富集失败不应影响取数主结果
            logger.warning("结果元信息（data_as_of/口径说明）富集失败，已跳过", exc_info=True)

        result_dict = payload.to_dict()
        result_dict["cached"] = False
        if cache is not None:
            # 落库时标记 cached=True，供下次命中后直接回显"缓存命中"
            stored = dict(result_dict)
            stored["cached"] = True
            cache.put(namespace, mq, row_scopes, stored)
        return result_dict

    # ---------------- 元信息 ----------------

    async def dimension_values(self, dim_name: str, limit: Optional[int] = None) -> List[str]:
        """维值候选（过滤条件召回用）：从 StarRocks 取该维度列的 DISTINCT 值。"""
        sql = self.compiler.compile_dimension_values(
            dim_name, limit or self.executor.settings.dim_value_recall_limit
        )
        result = await self.executor.fetch(sql)
        return [str(row[0]) for row in result["rows"] if row and row[0] is not None]

    async def health(self) -> Dict[str, Any]:
        return await self.executor.ping()

    async def probe(self, **kwargs: Any) -> Dict[str, Any]:
        """数据源深度探针（心跳 + 延迟 + 版本 + 各底表存量/最新时间）。

        见 ``semantic/probe.py``；参数（table_timeout/total_timeout/stale_days）
        透传，缺省由探针模块给。
        """
        from dmf_agent.semantic.probe import probe_datasource

        return await probe_datasource(self, **kwargs)

    async def data_as_of(self, model_names: List[str]) -> Optional[str]:
        """数据截至时间：对每个涉及的语义模型取时间维度列的最大值，返回其中最新者。

        纯探查用途（空数据回溯诊断用），失败静默返回 None，不抛错、不阻断主流程。
        """
        dates: List[str] = []
        models = [m for m in getattr(self.layer.manifest, "models", []) if m.name in set(model_names or [])]
        for model in models:
            td = model.time_dimension()
            if td is None:
                continue
            sql = f"SELECT MAX({td.expr}) FROM {model.table_ref}"
            try:
                res = await self.executor.fetch(sql)
                row = (res.get("rows") or [])[:1]
                if row and row[0] and row[0][0] is not None:
                    dates.append(str(row[0][0]))
            except Exception as exc:  # noqa: BLE001 - 探查失败不影响主流程
                logger.warning("data_as_of 探查失败（model=%s）：%s", model.name, exc)
        if not dates:
            return None
        try:
            return max(dates)
        except Exception:  # noqa: BLE001
            return dates[0]

    def describe(self) -> Dict[str, Any]:
        """语义层 + 引擎概览（管理接口/健康检查用）。"""
        s = self.executor.settings
        return {
            "engine": ENGINE_NAME,
            "manifest": str(self.layer.manifest.path),
            "starrocks": {
                "configured": s.configured,
                "target": f"{s.host}:{s.port}/{s.database}" if s.configured else "",
                "join_type": s.join_type,
                "max_rows": s.max_rows,
            },
            **self.layer.describe(),
        }


# --------------------------------------------------------------------------- #
# FR12 指标计算可解释性：口径说明（公式 / 时间窗 / 过滤 / 同环比 / 版本号）
#
# 设计口径：**全部确定性渲染，不调 LLM**。任何一次 LLM 复述都可能与真实口径漂移，
# 而"这次数字按什么口径算出来的"恰恰是最不能漂移的信息。LLM 只**消费**这里产出的
# 文本（见 nodes/synthesize_answer.py::_build_context 与 prompts 报告指令），
# 不参与生成；模板答复路径则直接附上确定性口径块。
# --------------------------------------------------------------------------- #

# 同环比类型 -> 中文口径说明。平移量必须与 compiler.lag_delta 一致（勿各自改）。
_POP_LABELS: Dict[str, str] = {
    "yoy": "同比（去年同期，平移 12 个月）",
    "mom": "环比（上一月，平移 1 个月）",
    "qoq": "环比（上一季度，平移 3 个月）",
    "wow": "周环比（上一周，平移 7 天）",
    "pop": "环比（上一周期，按当前粒度平移）",
}

_GRANULARITY_LABELS: Dict[str, str] = {
    "day": "日",
    "week": "周",
    "month": "月",
    "quarter": "季",
    "year": "年",
}

# 过滤运算符 -> 中文（取值域与 semantic/query.py::_OPERATOR_ALIASES 一致）
_OPERATOR_LABELS: Dict[str, str] = {
    "=": "等于",
    "!=": "不等于",
    ">": "大于",
    "<": "小于",
    ">=": "大于等于",
    "<=": "小于等于",
    "in": "属于",
    "not in": "不属于",
    "like": "包含",
}


def find_measure(manifest: Any, measure_name: str) -> Optional[Any]:
    """在语义清单的模型里找同名度量（simple 指标的公式需要它的 agg / expr）。"""
    if not measure_name:
        return None
    for model in getattr(manifest, "models", None) or []:
        getter = getattr(model, "measure", None)
        if not callable(getter):
            continue
        found = getter(measure_name)
        if found is not None:
            return found
    return None


def render_metric_formula(manifest: Any, metric: Any) -> str:
    """把指标定义渲染成一行可读的**计算口径公式**（FR12）。

    - simple ：``SUM(表列)``；带 ``two_stage`` 时展开为「逐日 inner -> 目标粒度 outer」
    - ratio  ：``分子 ÷ 分母``
    - derived：优先用定义里的 ``expr`` 原文（人工撰写的公式最贴近业务口径）
    """
    mtype = getattr(metric, "type", "") or ""
    if mtype == "ratio":
        return f"{metric.numerator or '?'} ÷ {metric.denominator or '?'}"
    if mtype == "derived":
        expr = str((getattr(metric, "type_params", None) or {}).get("expr") or "").strip()
        if expr:
            return expr
        names = [str(x) for x in (metric.input_metrics or [])]
        return "、".join(names) if names else "（未声明表达式）"
    measure_name = metric.measure or ""
    measure = find_measure(manifest, measure_name)
    agg = str(getattr(measure, "agg", "") or "").upper()
    expr = str(getattr(measure, "expr", None) or measure_name or "?")
    two_stage = getattr(metric, "two_stage", None) or {}
    if two_stage:
        inner = str(two_stage.get("inner") or (getattr(measure, "agg", None) or "sum")).upper()
        outer = str(two_stage.get("outer") or "avg").upper()
        return f"{inner}(逐日 {expr}) → {outer}(目标粒度)"
    if agg:
        return f"{agg}({expr})"
    return measure_name or "?"


def _filter_view(item: Any) -> Dict[str, Any]:
    dimension = str(getattr(item, "dimension", "") or "")
    operator = str(getattr(item, "operator", "") or "in")
    values = [str(v) for v in (getattr(item, "values", None) or [])]
    label = _OPERATOR_LABELS.get(operator, operator)
    return {
        "dimension": dimension,
        "operator": operator,
        "values": values,
        "text": f"{dimension or '?'} {label} {'、'.join(values)}".strip(),
    }


def _time_window_text(start: Optional[str], end: Optional[str], granularity: str) -> str:
    gran_label = _GRANULARITY_LABELS.get(str(granularity or ""), str(granularity or ""))
    if not start and not end:
        return f"未限定时间范围（按{gran_label}）" if gran_label else "未限定时间范围"
    span = f"{start or '不限'} ~ {end or '不限'}"
    return f"{span}（按{gran_label}）" if gran_label else span


def caliber_version(core: Dict[str, Any]) -> str:
    """口径版本号：对本次结果所用**定义内容**做稳定摘要（定义变更即换版本）。

    刻意不含时间窗 / 过滤条件等查询参数——同一次口径下的不同取数不应改变版本号；
    但含 description，因为口径**文案**修订同样属于口径变更，前端与答复都要能区分。
    """
    payload = json.dumps(core, ensure_ascii=False, sort_keys=True, default=str)
    return "v-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def _caliber_watermark_text(
    metrics_info: List[Dict[str, Any]],
    dims: List[str],
    models: List[str],
    data_as_of: Optional[str] = None,
    time_window_text: str = "",
    version: str = "",
) -> str:
    """把口径信息渲染成一行可读水印（前端直接展示，亦作结果落库的可追溯说明）。"""
    parts: List[str] = []
    for mi in metrics_info:
        name = mi.get("name") or "?"
        detail = mi.get("detail")
        parts.append(f"{name}（{detail}）" if detail else name)
    metric_str = "；".join(parts) if parts else "（无指标）"
    dim_str = "、".join(str(d) for d in dims) if dims else "无"
    model_str = "、".join(str(m) for m in models) if models else "未知"
    wm = f"口径：{metric_str}；维度={dim_str}；数据源={model_str}"
    if time_window_text:
        wm += f"；时间窗={time_window_text}"
    if data_as_of:
        wm += f"；数据截至={data_as_of}"
    if version:
        wm += f"；口径版本={version}"
    return wm


def build_caliber(
    manifest: Any,
    metric_names: List[str],
    dimension_names: List[str],
    model_names: List[str],
    data_as_of: Optional[str] = None,
    query: Optional[Any] = None,
    row_scopes: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, Any]:
    """构造**口径说明**：指标公式 + 维度 + 数据源 + 时间窗 + 过滤 + 同环比 + 版本号。

    供 ``SemanticQueryResult.caliber`` 透传到前端、审计与答复（FR12 / PRD 5.5），
    回答"这次结果到底按什么口径算出来的"。``query`` 缺省时只产出指标 / 维度 / 模型
    三块（兼容只按名称构造的调用方）。

    ``query`` 传 ``MetricQuery``：本模块只**读**它、不改，故用 ``Any`` 以免与
    ``semantic/query.py`` 形成类型耦合。
    """
    metrics_info: List[Dict[str, Any]] = []
    metric_by_name = {m.name: m for m in (getattr(manifest, "metrics", None) or [])}
    for name in metric_names or []:
        m = metric_by_name.get(name)
        if m is None:
            metrics_info.append(
                {
                    "name": name,
                    "type": "unknown",
                    "detail": "",
                    "formula": "",
                    "unit": "",
                    "description": "",
                }
            )
            continue
        if m.type == "simple":
            detail = f"度量={m.measure or ''}"
            if m.unit:
                detail += f"，单位={m.unit}"
        elif m.type == "ratio":
            detail = f"分子={m.numerator or ''}，分母={m.denominator or ''}"
        elif m.type == "derived":
            detail = f"子指标={','.join(m.input_metrics) or ''}"
        else:
            detail = ""
        metrics_info.append(
            {
                "name": m.name,
                "type": m.type,
                "detail": detail,
                "formula": render_metric_formula(manifest, m),
                "unit": m.unit or "",
                "scale": getattr(m, "scale", 1.0),
                "round": getattr(m, "round", 4),
                "description": m.description or "",
            }
        )
    dims = [str(d) for d in (dimension_names or [])]
    models = [str(x) for x in (model_names or [])]

    # ---- 查询侧要素：时间窗 / 过滤 / 同环比 / 聚合覆盖 / 排序 TopN ----
    time_window: Dict[str, Any] = {}
    filters: List[Dict[str, Any]] = []
    period_comparison: Dict[str, Any] = {"enabled": False, "types": [], "labels": []}
    aggregation_overrides: Dict[str, str] = {}
    metric_filters: List[str] = []
    post_processing: List[str] = []
    ranking: Dict[str, Any] = {}
    if query is not None:
        time_window = {
            "start": getattr(query, "start", None),
            "end": getattr(query, "end", None),
            "granularity": getattr(query, "granularity", "") or "",
            "granularity_label": _GRANULARITY_LABELS.get(
                str(getattr(query, "granularity", "") or ""), ""
            ),
            "text": _time_window_text(
                getattr(query, "start", None),
                getattr(query, "end", None),
                getattr(query, "granularity", "") or "",
            ),
        }
        filters = [_filter_view(f) for f in (getattr(query, "filters", None) or [])]
        types = [
            str(t)
            for t in (getattr(query, "period_comparison_types", None) or [])
            if str(t)
        ]
        if not types and getattr(query, "period_comparison_type", None):
            types = [str(query.period_comparison_type)]
        if types:
            period_comparison = {
                "enabled": True,
                "types": types,
                "labels": [_POP_LABELS.get(t, t) for t in types],
            }
        for name, op in (getattr(query, "metric_ops", None) or {}).items():
            agg_override = getattr(op, "agg_override", None)
            if agg_override:
                aggregation_overrides[str(name)] = str(agg_override)
            op_filter = getattr(op, "filter_operator", "") or ""
            op_value = getattr(op, "filter_value", None)
            if op_filter and op_value is not None:
                metric_filters.append(
                    f"{name} {_OPERATOR_LABELS.get(op_filter, op_filter)} {op_value}"
                )
            if getattr(op, "proportion", False):
                post_processing.append(f"{name}=占比")
            if getattr(op, "rank_group", False):
                post_processing.append(f"{name}=组内排名")
            elif getattr(op, "rank", False):
                post_processing.append(f"{name}=排名")
        ranking = {
            "order_by": getattr(query, "order_by", None),
            "limit": getattr(query, "limit", None),
            "rank_scope": getattr(query, "rank_scope", None) or "global",
        }

    # ---- 行权限范围（FR13）：让用户能解释"为什么我的数据比全行少" ----
    row_permission = {
        str(dim): sorted(str(v) for v in (values or []))
        for dim, values in (row_scopes or {}).items()
    }

    # 版本号只对**定义内容**取摘要：指标按名称排序，避免查询里指标书写顺序影响版本
    core = {
        "metrics": sorted(metrics_info, key=lambda x: str(x.get("name") or "")),
        "dimensions": sorted(dims),
        "models": sorted(models),
    }
    version = caliber_version(core)

    payload: Dict[str, Any] = {
        "version": version,
        "metrics": metrics_info,
        "dimensions": dims,
        "models": models,
        "data_as_of": data_as_of,
        "time_window": time_window,
        "filters": filters,
        "period_comparison": period_comparison,
        "aggregation_overrides": aggregation_overrides,
        "metric_filters": metric_filters,
        "post_processing": post_processing,
        "ranking": ranking,
        "row_permission": row_permission,
    }
    payload["watermark"] = _caliber_watermark_text(
        metrics_info,
        dims,
        models,
        data_as_of,
        time_window_text=str(time_window.get("text") or ""),
        version=version,
    )
    return payload


def caliber_lines(caliber: Dict[str, Any]) -> List[str]:
    """口径说明的确定性条目（标签 -> 值），答复附注与前端展示共用同一口径。"""
    if not caliber:
        return []
    lines: List[str] = []
    for mi in caliber.get("metrics") or []:
        name = mi.get("name") or "?"
        formula = str(mi.get("formula") or mi.get("detail") or "").strip()
        unit = str(mi.get("unit") or "")
        extra = f"，单位={unit}" if unit else ""
        lines.append(f"指标「{name}」：{formula}{extra}" if formula else f"指标「{name}」{extra}")
    tw = (caliber.get("time_window") or {}).get("text")
    if tw:
        lines.append(f"时间窗：{tw}")
    texts = [str(f.get("text")) for f in (caliber.get("filters") or []) if f.get("text")]
    if texts:
        lines.append("过滤条件：" + "；".join(texts))
    labels = (caliber.get("period_comparison") or {}).get("labels") or []
    if labels:
        lines.append("同环比：" + "、".join(str(x) for x in labels))
    overrides = caliber.get("aggregation_overrides") or {}
    if overrides:
        lines.append("聚合覆盖：" + "、".join(f"{k}={v}" for k, v in overrides.items()))
    if caliber.get("metric_filters"):
        lines.append("指标阈值：" + "；".join(str(x) for x in caliber["metric_filters"]))
    if caliber.get("post_processing"):
        lines.append("二次分析：" + "、".join(str(x) for x in caliber["post_processing"]))
    rank = caliber.get("ranking") or {}
    rank_parts: List[str] = []
    if rank.get("order_by"):
        rank_parts.append(f"排序={rank['order_by']}")
    if rank.get("limit"):
        rank_parts.append(f"取前 {rank['limit']} 条")
    if rank.get("rank_scope") == "group":
        rank_parts.append("按时间列组内排名")
    if rank_parts:
        lines.append("排序/TopN：" + "，".join(rank_parts))
    row_perm = caliber.get("row_permission") or {}
    if row_perm:
        lines.append(
            "行权限范围：" + "；".join(f"{k}∈{'、'.join(v)}" for k, v in row_perm.items())
        )
    if caliber.get("models"):
        lines.append("数据源：" + "、".join(str(x) for x in caliber["models"]))
    if caliber.get("data_as_of"):
        lines.append(f"数据截至：{caliber['data_as_of']}")
    if caliber.get("version"):
        lines.append(f"口径版本：{caliber['version']}")
    return lines


def caliber_context_text(caliber: Dict[str, Any]) -> str:
    """渲染为 LLM 上下文片段：把口径作为**事实**交给模型引用，而非让它自己复述定义。"""
    lines = caliber_lines(caliber)
    return "\n".join(f"- {x}" for x in lines) if lines else ""


def caliber_markdown(caliber: Dict[str, Any]) -> str:
    """渲染为答复末尾的确定性口径附注（模板答复路径用；LLM 路径靠上下文 + 前端块）。"""
    lines = caliber_lines(caliber)
    if not lines:
        return ""
    body = "\n".join(f"- {x}" for x in lines)
    return f"\n\n**本次结果口径说明**（可追溯，答案数字即按此口径计算）\n{body}\n"


__all__ = [
    "ENGINE_NAME",
    "MetricFlowEngine",
    "SemanticQueryResult",
    "StarRocksError",
    "build_caliber",
    "caliber_context_text",
    "caliber_lines",
    "caliber_markdown",
    "caliber_version",
    "find_measure",
    "render_metric_formula",
]


