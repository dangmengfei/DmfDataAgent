"""语义层：主流程唯一的元信息入口（指标 / 维度 / 别名 / 粒度 / 同环比白名单）。

设计要点：
- 一切业务语义来自 manifest.yml（MetricFlow 规范），语义层不感知任何外部指标平台编码；
- Agent 节点只通过本模块解析指标与维度，物理表/列只出现在清单里（由 compiler 使用）；
- 维度可用性按"指标的宿主语义模型"判定——某模型没有的维度，其指标也不支持按该维度分组
  （对齐 MetricFlow 的 group-by item 校验语义）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from dmf_agent.semantic.manifest import (
    MEASURE_AGGS,
    METRIC_TYPES,
    POP_TYPES,
    TIME_GRANULARITIES,
    Dimension,
    Measure,
    Metric,
    SemanticManifest,
    SemanticModel,
)

__all__ = [
    "MEASURE_AGGS",
    "METRIC_TYPES",
    "POP_TYPES",
    "TIME_GRANULARITIES",
    "Dimension",
    "Measure",
    "Metric",
    "SemanticLayer",
    "SemanticModel",
]


class SemanticLayer:
    def __init__(self, manifest_path: Optional[str | Path] = None) -> None:
        self.manifest = SemanticManifest(manifest_path)

    @classmethod
    def from_manifest_dict(cls, raw: Dict[str, Any]) -> "SemanticLayer":
        """从已合并的清单字典构造（用于 DB 发布快照重建，不读文件）。"""
        layer = cls.__new__(cls)
        layer.manifest = SemanticManifest.from_dict(raw)
        return layer

    # ---------------- 指标 ----------------

    def list_metrics(self) -> List[Metric]:
        return list(self.manifest.metrics)

    def metric_names(self) -> List[str]:
        return [m.name for m in self.manifest.metrics]

    def metric_search_keys(self) -> List[str]:
        """指标名 + 别名（候选召回用）。"""
        keys: List[str] = []
        for metric in self.manifest.metrics:
            keys.append(metric.name)
            keys.extend(metric.aliases)
        return keys

    def resolve_metric(self, name: str) -> Optional[Metric]:
        return self.manifest.resolve_metric(name)

    # ---------------- 维度 ----------------

    def list_dimensions(self) -> List[Dimension]:
        return self.manifest.dimensions()

    def dimension_names(self) -> List[str]:
        return [d.name for d in self.list_dimensions()]

    def resolve_dimension(self, name: str) -> Optional[Dimension]:
        return self.manifest.dimension(name)

    def time_dimension_name(self) -> Optional[str]:
        dim = self.manifest.time_dimension()
        return dim.name if dim is not None else None

    def dimension_supported(self, metric: Metric, dim_name: str) -> bool:
        """指标是否支持按该维度分组（宿主模型需提供该维度）。"""
        model = self._metric_model(metric)
        if model is None:
            return False
        return model.dimension(dim_name) is not None

    def metric_models(self, metric: Metric) -> List[SemanticModel]:
        """指标（含 ratio/derived 的引用链）涉及的语义模型。"""
        models: List[SemanticModel] = []
        for base in self.base_measures(metric.name):
            model = self.manifest.measure_owner(base)
            if model is not None and model not in models:
                models.append(model)
        return models

    def _metric_model(self, metric: Metric) -> Optional[SemanticModel]:
        models = self.metric_models(metric)
        return models[0] if models else None

    # ---------------- 指标 -> measure 展开 ----------------

    def base_measures(self, metric_name: str) -> List[str]:
        """把指标展开到 measure 层（simple 自身；ratio/derived 递归展开引用指标）。

        展开结果去重保序；未定义的引用直接忽略（结构性错误已由清单校验拦截）。
        """
        out: List[str] = []

        def walk(name: str) -> None:
            metric = self.manifest.metric(name)
            if metric is None:
                return
            if metric.type == "simple":
                measure = metric.measure
                if measure and measure not in out:
                    out.append(measure)
                return
            if metric.type == "ratio":
                for ref in (metric.numerator, metric.denominator):
                    if not ref:
                        continue
                    if self.manifest.metric(ref) is not None:
                        walk(ref)
                    elif ref not in out:  # 允许直接写 measure 名
                        out.append(ref)
                return
            for ref in metric.input_metrics:
                walk(ref)

        walk(metric_name)
        return out

    def primary_model_of_metric(self, metric: Metric) -> Optional[SemanticModel]:
        """指标宿主模型：simple 取其 measure 的模型；复合指标取引用链首个模型。"""
        return self._metric_model(metric)

    # ---------------- 校验 ----------------

    def validate_metric_support(
        self,
        metric: Metric,
        granularity: Optional[str] = None,
        pop_type: Optional[str] = None,
    ) -> List[str]:
        """粒度 / 同环比类型白名单校验（替代原 HM 侧 STATTIME_2_POP 硬编码）。"""
        problems: List[str] = []
        if granularity and granularity not in TIME_GRANULARITIES:
            problems.append(
                f"不支持的时间粒度 {granularity}（支持：{'/'.join(TIME_GRANULARITIES)}）"
            )
        elif granularity and granularity not in metric.granularities:
            problems.append(
                f"指标【{metric.name}】不支持时间粒度 {granularity}"
                f"（支持：{'/'.join(metric.granularities)}）"
            )
        if pop_type and pop_type not in POP_TYPES:
            problems.append(f"不支持的同环比类型 {pop_type}（支持：{'/'.join(POP_TYPES)}）")
        elif pop_type and pop_type not in metric.pop_types:
            problems.append(
                f"指标【{metric.name}】不支持同环比类型 {pop_type}"
                f"（支持：{'/'.join(metric.pop_types)}）"
            )
        return problems

    def validate_elements(self, elements: Dict[str, Any]) -> List[str]:
        """查询要素校验：指标是否收录、维度是否被指标支持、粒度/同环比是否在支持范围。

        返回问题列表（空 = 通过），供 validate_elements 节点直接作为追问理由。
        """
        problems: List[str] = []
        names = elements.get("metrics") or []
        if not names:
            return ["未能从问题中识别出任何业务指标"]

        resolved: List[Metric] = []
        for name in names:
            metric = self.resolve_metric(name)
            if metric is None:
                problems.append(f"指标[{name}]未收录在语义层")
            else:
                resolved.append(metric)
        if not resolved:
            return problems

        time_range = elements.get("time_range") or {}
        granularity = time_range.get("granularity")
        # FR1：同环比可并存 -> 逐类型校验（单类型时与旧行为完全一致）
        pop_types = [
            str(t).strip().lower()
            for t in (elements.get("period_comparison_types") or [])
            if str(t).strip()
        ] or ([str(elements["period_comparison_type"]).lower()] if elements.get("period_comparison_type") else [])

        # 分组时间维度不参与"维度可用性"判定（时间维度要求各模型同名，由 compiler 校验）
        time_dim_name = self.time_dimension_name()
        for dim in elements.get("dimensions") or []:
            if dim == time_dim_name:
                continue
            if self.resolve_dimension(dim) is None:
                problems.append(
                    f"维度[{dim}]未收录在语义层"
                    f"（可用：{'/'.join(self.dimension_names())}）"
                )
                continue
            unsupported = [m.name for m in resolved if not self.dimension_supported(m, dim)]
            if unsupported:
                problems.append(
                    f"维度[{dim}]不被指标（{'、'.join(unsupported)}）所在数据域支持"
                    f"（该指标可用维度："
                    f"{'/'.join(self.supported_dimensions(resolved[0]))}）"
                )

        for metric in resolved:
            seen_problems: List[str] = []
            for pop_type in pop_types:
                for problem in self.validate_metric_support(metric, granularity, pop_type):
                    if problem not in seen_problems:
                        seen_problems.append(problem)
            problems.extend(seen_problems)
        return problems

    def supported_dimensions(self, metric: Metric) -> List[str]:
        """指标可用的维度名（宿主模型维度 ∩ 全局维度；复合指标取所有涉及模型交集）。"""
        models = self.metric_models(metric)
        if not models:
            return []
        common = {d.name for d in models[0].dimensions}
        for model in models[1:]:
            common &= {d.name for d in model.dimensions}
        return [d.name for d in models[0].dimensions if d.name in common]

    # ---------------- 描述 ----------------

    def describe(self) -> Dict[str, Any]:
        """语义层概览（供健康检查 / 管理接口）。"""
        return {
            "manifest": str(self.manifest.path),
            "semantic_models": [
                {
                    "name": m.name,
                    "table": m.table,
                    "description": m.description,
                    "dimensions": [d.name for d in m.dimensions],
                    "measures": [x.name for x in m.measures],
                }
                for m in self.manifest.models
            ],
            "metrics": [
                {
                    "name": m.name,
                    "type": m.type,
                    "unit": m.unit,
                    "scale": m.scale,
                    "round": m.round,
                    "aliases": m.aliases,
                    "granularities": m.granularities,
                    "pop_types": m.pop_types,
                    "base_measures": self.base_measures(m.name),
                    "dimensions": self.supported_dimensions(m),
                    "description": m.description,
                }
                for m in self.manifest.metrics
            ],
        }
