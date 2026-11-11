"""语义层 API：语义查询（MetricFlow 编译 -> StarRocks 执行）与语义清单浏览。

Agent 与前端共用同一入口：
- /semantic/query    语义查询（返回 columns/rows/sql/耗时）
- /semantic/compile  只编译（联调/排查"这条问题会生成什么 SQL"）
- /semantic/metrics  指标目录（含别名/支持维度/粒度/同环比）
- /semantic/dimensions 维度目录
- /semantic/manifest 完整语义清单概览（语义模型 -> 物理表）
- /semantic/health   StarRocks 连通性探测
- /semantic/knowledge-graph  语义血缘图谱（指标/维度/模型/度量 + 关系，可聚焦某指标）
- /semantic/knowledge-graph/lineage  指标血缘文本（RAG 检索增强上下文）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_SEMANTIC, PERM_SEMANTIC_QUERY
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.semantic.compiler import CompileError
from dmf_agent.semantic.scope import RequestScope
from dmf_agent.semantic.starrocks import StarRocksError
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()

logger = logging.getLogger(__name__)


def _guard(permission: str) -> list:
    """路由级权限点依赖：读语义元信息 / 执行语义查询分档（admin 通配放行）。"""
    return [Depends(require_permission(permission))]


class SemanticFilter(BaseModel):
    field: str
    operator: str = "in"
    values: List[str] = Field(default_factory=list)


class SemanticTimeRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    granularity: Optional[str] = "day"


class SemanticQueryRequest(BaseModel):
    metrics: List[str] = Field(..., min_length=1, description="业务指标名 / 别名")
    group_by: List[str] = Field(default_factory=list, description="分组维度（含时间维度名）")
    filters: List[SemanticFilter] = Field(default_factory=list)
    time_range: Optional[SemanticTimeRange] = None
    period_comparison: bool = False
    period_comparison_type: Optional[str] = None
    # FR1：同环比可并存（同比 + 环比）-> 多个平移窗口各自输出增长值/增长率
    period_comparison_types: List[str] = Field(
        default_factory=list, description="同环比类型列表，可并存（如 ['yoy','mom']）"
    )
    order_by: Optional[str] = None
    limit: Optional[int] = None
    # FR1 排名范围：global 全局排名（默认）| group 时间序列组内排名
    rank_scope: Optional[str] = Field(
        None, description="排名范围：global 全局 | group 组内（按时间周期分别排名）"
    )
    # 二次分析（透传 MetricQuery.from_dsl 的 advanced）：
    #   {"metric_result": [{"metric_name": M, "agg": "avg", "proportion": true,
    #                       "orderby": "desc", "limit": "5",
    #                       "filter_operator": ">", "condition_value": 0}]}
    # 承载"时间限定型派生"（日均 = agg 覆盖 avg）与占比 / 排名 / top-n。
    # 不加这个字段时 Pydantic 会静默丢弃 advanced，能力只在 Agent/CLI 侧可用。
    advanced: Optional[Dict[str, Any]] = Field(
        default=None, description="二次分析配置（聚合覆盖 / 占比 / 排名 / top-n）"
    )


def _require_engine(runtime: AppRuntime):
    if runtime.metric_engine is None:
        raise HTTPException(status_code=503, detail="语义引擎未装配")
    return runtime.metric_engine


async def _request_scope(
    runtime: AppRuntime, current: CurrentUser
) -> Optional[RequestScope]:
    """当前用户租户的语义层作用域；未装配解析服务 / 解析失败时返回 None。

    返回 None 表示"用进程级全局层"（鉴权关闭时 tenant_id 为空，天然走这条路），
    调用方据此回落，绝不因为多租户解析而让接口不可用。
    """
    service = getattr(runtime, "semantic_scope", None)
    tenant_id = (getattr(current, "tenant_id", "") or "").strip()
    if service is None or not tenant_id:
        return None
    try:
        return await service.scope_for(tenant_id)
    except Exception:  # noqa: BLE001 - 租户解析失败不阻断接口
        logger.warning("语义层租户作用域解析失败（回落全局层）", exc_info=True)
        return None


async def _engine_for(runtime: AppRuntime, current: CurrentUser):
    """当前租户的语义引擎（编译 + 取数口径随租户 overlay 变化）。"""
    scope = await _request_scope(runtime, current)
    engine = scope.engine if scope is not None else runtime.metric_engine
    if engine is None:
        raise HTTPException(status_code=503, detail="语义引擎未装配")
    return engine


async def _layer_for(runtime: AppRuntime, current: CurrentUser):
    """当前租户的语义层（指标/维度目录随租户 overlay 变化）。"""
    scope = await _request_scope(runtime, current)
    layer = scope.layer if scope is not None else runtime.semantic_layer
    if layer is None:
        raise HTTPException(status_code=503, detail="语义层未配置")
    return layer


def _metrics_payload(layer) -> List[Dict[str, Any]]:
    return [
        {
            "name": m.name,
            "type": m.type,
            "unit": m.unit,
            "aliases": m.aliases,
            "granularities": m.granularities,
            "pop_types": m.pop_types,
            "scale": m.scale,
            "round": m.round,
            "base_measures": layer.base_measures(m.name),
            "dimensions": layer.supported_dimensions(m),
            "models": [x.name for x in layer.metric_models(m)],
            "description": m.description,
        }
        for m in layer.list_metrics()
    ]


@router.post(
    "/semantic/query",
    summary="语义查询（MetricFlow 编译 SQL -> StarRocks 执行）",
    # 执行查询会真实打底表（资源开销），归入写侧能力：viewer 不可执行
    dependencies=_guard(PERM_SEMANTIC_QUERY),
)
async def semantic_query(
    body: SemanticQueryRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    engine = await _engine_for(runtime, current)
    payload = body.model_dump()
    if body.limit:
        payload["order_by"] = f"{payload.get('order_by') or ''} limit {body.limit}".strip()
    try:
        return await engine.query(payload)
    except CompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StarRocksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/semantic/compile",
    summary="只编译 SQL（不执行）",
    # 编译只读语义元信息、不触达底表：viewer 也可用（只读能力）
    dependencies=_guard(PERM_SEMANTIC),
)
async def semantic_compile(
    body: SemanticQueryRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    engine = await _engine_for(runtime, current)
    try:
        compiled = engine.compile(body.model_dump())
    except CompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "sql": compiled.sql,
        "columns": compiled.columns,
        "group_columns": compiled.group_columns,
        "metric_columns": compiled.metric_columns,
        "models": compiled.models,
        "engine": "metricflow",
    }


@router.get("/semantic/metrics", summary="语义层指标目录", dependencies=_guard(PERM_SEMANTIC))
async def semantic_metrics(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[Dict[str, Any]]:
    layer = await _layer_for(runtime, current)
    return _metrics_payload(layer)


@router.get("/semantic/dimensions", summary="语义层维度目录", dependencies=_guard(PERM_SEMANTIC))
async def semantic_dimensions(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[Dict[str, Any]]:
    layer = await _layer_for(runtime, current)
    return [
        {
            "name": d.name,
            "type": d.type,
            "expr": d.expr,
            "time_granularity": d.time_granularity,
            "models": [m.name for m in layer.manifest.dimension_owners(d.name)],
        }
        for d in layer.list_dimensions()
    ]


@router.get("/semantic/manifest", summary="语义清单概览（语义模型 -> 物理表）", dependencies=_guard(PERM_SEMANTIC))
async def semantic_manifest(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    engine = await _engine_for(runtime, current)
    return engine.describe()


@router.get("/semantic/health", summary="数据源连通性深度探针（心跳/延迟/表存量）", dependencies=_guard(PERM_SEMANTIC))
async def semantic_health(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    engine = _require_engine(runtime)
    # 深度探针：心跳 + 延迟 + 版本 + 各底表 COUNT(*)/时间维度最新值 + data_as_of。
    # 探针内部逐项容错且不抛错；结果缓存到 runtime.datasource_status 供前端徽标消费。
    h = await engine.probe()
    runtime.datasource_status = h
    return h


@router.get(
    "/semantic/knowledge-graph",
    summary="语义血缘图谱（指标/维度/模型/度量 + 关系）",
    dependencies=_guard(PERM_SEMANTIC),
)
async def semantic_knowledge_graph(
    focus: Optional[str] = None,
    hops: int = 1,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """语义层确定性血缘图。

    - 不传 ``focus``：返回全量图（节点 = 指标/维度/模型/度量，边 = 基于/计算自/归属/含/派生自）；
    - 传 ``focus=指标名``：返回围绕该指标的 ``hops`` 跳子图（聚焦血缘）。
    """
    layer = await _layer_for(runtime, current)
    from dmf_agent.rag.knowledge_graph import (
        build_knowledge_graph,
        subgraph_for_metrics,
    )

    graph = (
        subgraph_for_metrics(layer, [focus], hops=hops)
        if focus
        else build_knowledge_graph(layer)
    )
    return {
        "focus": focus,
        "node_types": {
            "metric": "指标",
            "dimension": "维度",
            "model": "语义模型",
            "measure": "度量",
        },
        "edge_labels": {
            "based_on": "基于",
            "computed_from": "计算自",
            "owned_by": "归属模型",
            "has_dimension": "含维度",
            "derived_from": "派生自",
        },
        "graph": graph.to_dict(),
    }


@router.get(
    "/semantic/knowledge-graph/lineage",
    summary="指标血缘文本（RAG 检索增强上下文）",
    dependencies=_guard(PERM_SEMANTIC),
)
async def semantic_knowledge_lineage(
    query: str = "",
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """从自然语言问句抽取命中指标并展开血缘文本，作为 RAG 检索增强的常识上下文。

    返回 ``{metrics, text}``；未命中任何指标时 ``metrics=[] / text=""``。
    """
    layer = await _layer_for(runtime, current)
    from dmf_agent.rag.knowledge_graph import (
        extract_metric_names_from_query,
        lineage_text,
    )

    metrics = extract_metric_names_from_query(layer, query)
    return {"metrics": metrics, "text": lineage_text(layer, metrics)}
