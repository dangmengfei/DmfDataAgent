"""语义注册中心种子构建：`manifest.yml`（SemanticLayer）→ 注册中心种子结构。

metric_key 推导规则（P1 确定性生成，manifest 无需显式写 key）：
- simple  → measure 名（`gmv_amount`、`retail_deposit_balance`，已是英文 snake_case）；
- ratio   → `{分子key}__{分母key}_ratio`（引用指标递归取 key，直接引用 measure 则用 measure 名）；
- derived → `derived_{md5(name)[:8]}`（表达式中文名，无法自然转 slug，取稳定短哈希）。

P2 起 manifest.yml 可显式写 `key:` 覆盖推导值，`semantic check` 增加"缺 key 即报错"。

dimension_key：取物理 `expr`（`channel_name` 等英文列名，稳定），兜底 name。
model_key / measure_key：直接用 manifest 的模型名 / 度量名（英文）。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from dmf_agent.semantic.layer import SemanticLayer

__all__ = ["build_registry_seed", "derive_metric_key", "derive_dimension_key"]


def _ref_key(ref: str, layer: SemanticLayer, memo: Optional[Dict[str, str]] = None) -> str:
    """引用（指标名或 measure 名）→ 稳定 key：指标递归取其 key，measure 用自身名。"""
    memo = memo if memo is not None else {}
    if ref in memo:
        return memo[ref]
    metric = layer.resolve_metric(ref)
    if metric is None:
        # 直接引用 measure（ratio 允许 numerator/denominator 写 measure 名）
        return ref
    key = derive_metric_key(metric.name, layer, memo)
    memo[ref] = key
    return key


def derive_dimension_key(dim) -> str:
    key = (getattr(dim, "expr", "") or "").strip()
    return key or (getattr(dim, "name", "") or "").strip()


def derive_metric_key(
    name: str, layer: SemanticLayer, memo: Optional[Dict[str, str]] = None
) -> str:
    memo = memo if memo is not None else {}
    if name in memo:
        return memo[name]
    metric = layer.resolve_metric(name)
    if metric is None:
        raise ValueError(f"指标未收录：{name}")
    if metric.type == "simple":
        key = metric.measure or name
    elif metric.type == "ratio":
        num = _ref_key(metric.numerator or "", layer, memo)
        den = _ref_key(metric.denominator or "", layer, memo)
        key = f"{num}__{den}_ratio"
    else:
        key = f"derived_{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}"
    memo[name] = key
    return key


def _model_payload(m, layer: SemanticLayer) -> Dict[str, Any]:
    return {
        "model_key": m.name,
        "name": m.name,
        "description": m.description or "",
        "table": m.table,
        "agg_time_dimension": m.agg_time_dimension or "",
        "entities": [
            {"name": e.name, "entity_type": e.type, "expr": e.expr} for e in m.entities
        ],
        "dimensions": [
            {
                "dimension_key": derive_dimension_key(d),
                "name": d.name,
                "dim_type": d.type,
                "expr": d.expr,
                "time_granularity": d.time_granularity or "day",
                "description": d.description or "",
            }
            for d in m.dimensions
        ],
        "measures": [
            {
                "measure_key": x.name,
                "name": x.name,
                "agg": x.agg,
                "expr": x.expr,
                "agg_time_dimension": x.agg_time_dimension or "",
                "unit": x.unit or "",
                "description": x.description or "",
            }
            for x in m.measures
        ],
        "status": "published",
        "source": "builtin",
    }


def _metric_payload(m, layer: SemanticLayer, memo: Dict[str, str]) -> Dict[str, Any]:
    return {
        "metric_key": derive_metric_key(m.name, layer, memo),
        "name": m.name,
        "metric_type": m.type,
        "type_params": dict(m.type_params or {}),
        "aliases": list(m.aliases or []),
        "unit": m.unit or "",
        "description": m.description or "",
        "scale": float(m.scale or 1.0),
        "round": int(m.round or 4),
        "granularities": list(m.granularities or []),
        "pop_types": list(m.pop_types or []),
        "status": "published",
        "source": "builtin",
    }


def build_registry_seed(layer: SemanticLayer) -> Dict[str, Any]:
    """从语义层（manifest.yml）构建注册中心种子（幂等导入用）。"""
    memo: Dict[str, str] = {}
    models: List[Dict[str, Any]] = [_model_payload(m, layer) for m in layer.manifest.models]
    metrics: List[Dict[str, Any]] = [
        _metric_payload(m, layer, memo) for m in layer.list_metrics()
    ]
    return {"models": models, "metrics": metrics}
