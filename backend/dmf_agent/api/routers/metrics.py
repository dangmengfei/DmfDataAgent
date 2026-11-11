"""指标目录接口（前端"指标市场"用）。

2026-09 主流程脱离指标平台后，指标元信息唯一来源为**语义层**（manifest.yml），
本路由不再读取指标平台元数据仓储（runtime.db.metric_repo），响应结构保持不变
以兼容前端：
- code        -> 语义层标准指标名（稳定标识）
- category    -> 指标类型（simple / ratio / derived）
- dimensions  -> 该指标支持的维度名（宿主语义模型 ∩ 全局维度）
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.metrics import MetricData
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()

logger = logging.getLogger(__name__)


def _require_layer(runtime: AppRuntime) -> SemanticLayer:
    if runtime.semantic_layer is None:
        raise HTTPException(status_code=503, detail="语义层未配置")
    return runtime.semantic_layer


async def _layer_for(runtime: AppRuntime, current: CurrentUser) -> SemanticLayer:
    """当前用户租户的语义层；未装配 / 无租户 / 解析失败时回落进程级全局层。

    指标市场按租户 overlay 呈现：租户自定义的指标口径与别名应当被本租户看到，
    而别的租户自己的口径不应泄露进来。
    """
    service = getattr(runtime, "semantic_scope", None)
    tenant_id = (getattr(current, "tenant_id", "") or "").strip()
    if service is not None and tenant_id:
        try:
            scope = await service.scope_for(tenant_id)
            if scope is not None and scope.layer is not None:
                return scope.layer
        except Exception:  # noqa: BLE001 - 租户解析失败不阻断指标市场
            logger.warning("指标目录租户语义层解析失败（回落全局层）", exc_info=True)
    return _require_layer(runtime)


def _to_metric_data(layer: SemanticLayer, metric) -> MetricData:
    return MetricData(
        code=metric.name,
        name=metric.name,
        description=metric.description,
        unit=metric.unit,
        category=metric.type,
        dimensions=layer.supported_dimensions(metric),
    )


@router.get("/metrics", response_model=List[MetricData], summary="指标检索（语义层）")
async def search_metrics(
    keyword: Optional[str] = None,
    limit: int = 20,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[MetricData]:
    layer = await _layer_for(runtime, current)
    metrics = layer.list_metrics()
    if keyword:
        needle = keyword.strip().lower()
        metrics = [
            m
            for m in metrics
            if needle in m.name.lower()
            or any(needle in alias.lower() for alias in m.aliases)
        ]
    return [_to_metric_data(layer, m) for m in metrics[: max(0, limit)]]


@router.get("/metrics/{code}", response_model=MetricData, summary="指标详情（语义层）")
async def get_metric(
    code: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> MetricData:
    layer = await _layer_for(runtime, current)
    metric = layer.resolve_metric(code)  # 支持标准名与别名
    if metric is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    return _to_metric_data(layer, metric)
