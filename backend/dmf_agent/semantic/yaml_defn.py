"""指标口径 → 语义模型 YAML dict，并构造含该指标的临时语义层副本。

为什么需要这份能力（PRD 5.9 完整闭环）
--------------------------------
`runtime.semantic_layer` 只从 `manifest.d/` + DB 发布快照装配，**不含** `SemanticMetricModel`
行。于是「刚发布的指标」在发布那一刻既不在运行态语义层、也不在知识库口径字典里 —— 这是
PRD 5.9 闭环缺口 (a)。为校验/重建拿到「含新指标」的语义层，又不污染运行时层，这里提供：

- `metric_to_yaml_defn(brief)`：从指标 brief 抽取 manifest 兼容的口径 dict，持久化到
  `SemanticMetricModel.yaml_defn` 列（可观测、可回放、可重建快照）。
- `build_layer_with_metric(base_layer, brief)`：把新指标并入 `base_layer` 的合并 dict，
  构造**独立副本层**（`SemanticLayer.from_manifest_dict`），仅供发布期 ④ SQL dry-run /
  ⑥ 试跑 / 知识库重建使用，绝不写回运行时层。

设计原则：best-effort、安全降级。构造失败（base 层缺失 / 合并后清单非法）一律返回 None，
由调用方回落到 base 层或跳过深度校验，绝不阻断发布主链路。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

from dmf_agent.semantic.layer import SemanticLayer

logger = logging.getLogger(__name__)


def metric_to_yaml_defn(brief: Dict[str, Any]) -> Dict[str, Any]:
    """从指标 brief 抽取 manifest 兼容的口径 dict。

    仅描述「指标」本身（其依赖的 measure / 模型来自已存在的语义层，不重复生成模型）。
    形如：
    {
      "metric": {
        "name", "type", "type_params", "aliases", "unit",
        "description", "scale", "round", "granularities", "pop_types"
      }
    }
    """
    return {
        "metric": {
            "name": brief.get("name", ""),
            "type": brief.get("metric_type", "simple"),
            "type_params": dict(brief.get("type_params") or {}),
            "aliases": list(brief.get("aliases") or []),
            "unit": brief.get("unit", ""),
            "description": brief.get("description", ""),
            "scale": float(brief.get("scale", 1.0) or 1.0),
            "round": int(brief.get("round", 4) or 4),
            "granularities": list(brief.get("granularities") or []),
            "pop_types": list(brief.get("pop_types") or []),
        }
    }


def _merged_manifest_dict(
    base_layer: SemanticLayer, brief: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """把新指标并入 base 层清单 dict（深拷贝，不污染入参）。失败返回 None。"""
    try:
        raw = copy.deepcopy(base_layer.manifest.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 base 语义层清单失败：%s", exc)
        return None
    metrics = raw.setdefault("metrics", [])
    mdef = metric_to_yaml_defn(brief)["metric"]
    # 同名指标已存在则覆盖式更新（重发布同一口径），否则追加
    for i, existing in enumerate(metrics):
        if existing.get("name") == mdef["name"]:
            metrics[i] = mdef
            break
    else:
        metrics.append(mdef)
    return raw


def build_layer_with_metric(
    base_layer: Optional[SemanticLayer],
    brief: Dict[str, Any],
) -> Optional[SemanticLayer]:
    """构造「含新指标」的语义层副本（不污染运行时层）。

    失败（base 层缺失 / 合并后清单校验非法）降级返回 None，由调用方回落 base 层或跳过深度校验。
    """
    if base_layer is None:
        return None
    raw = _merged_manifest_dict(base_layer, brief)
    if raw is None:
        return None
    try:
        return SemanticLayer.from_manifest_dict(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("合并层构造失败（回落 base 层）：%s", exc)
        return None


def yaml_defn_to_metric_dict(yaml_defn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从落库的 yaml_defn 还原指标 manifest dict（重建快照 / 可观测用）。缺失返回空 dict。"""
    if not yaml_defn:
        return {}
    m = yaml_defn.get("metric") or {}
    return dict(m)
