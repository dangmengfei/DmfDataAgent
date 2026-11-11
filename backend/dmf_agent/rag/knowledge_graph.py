"""语义血缘知识图谱（Task D）。

从语义层（SemanticLayer）**确定性**构图：节点 = 指标 / 维度 / 语义模型 / 度量；
边 = 指标→模型（基于）、指标→度量（计算自）、度量→模型（归属）、模型→维度（含）、
指标→指标（派生自，ratio/derived）。构图完全由语义层决定，幂等、可重放。

用途：
1. 血缘可视化：给前端提供关系图（ECharts graph）展示指标如何从底层表/度量支撑；
2. 检索增强（RAG）：把问题命中的指标血缘展开成文本上下文，注入检索结果
   （见 :func:`lineage_text` 与 :func:`extract_metric_names_from_query`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# ---- 节点类型 ----
NODE_METRIC = "metric"
NODE_DIMENSION = "dimension"
NODE_MODEL = "model"
NODE_MEASURE = "measure"

NODE_TYPES = {
    NODE_METRIC: "指标",
    NODE_DIMENSION: "维度",
    NODE_MODEL: "语义模型",
    NODE_MEASURE: "度量",
}

# ---- 边类型 ----
EDGE_BASED_ON = "based_on"            # metric -> model
EDGE_COMPUTED_FROM = "computed_from"  # metric -> measure
EDGE_OWNED_BY = "owned_by"            # measure -> model
EDGE_HAS_DIMENSION = "has_dimension"  # model -> dimension
EDGE_DERIVED_FROM = "derived_from"    # metric -> metric

EDGE_LABELS = {
    EDGE_BASED_ON: "基于",
    EDGE_COMPUTED_FROM: "计算自",
    EDGE_OWNED_BY: "归属模型",
    EDGE_HAS_DIMENSION: "含维度",
    EDGE_DERIVED_FROM: "派生自",
}


@dataclass
class GraphNode:
    id: str
    type: str
    label: str
    props: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str
    label: str = ""


@dataclass
class KnowledgeGraph:
    nodes: List[GraphNode]
    edges: List[GraphEdge]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {"id": n.id, "type": n.type, "label": n.label, "props": n.props}
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "type": e.type,
                    "label": e.label,
                }
                for e in self.edges
            ],
        }


def _node_id(node_type: str, name: str) -> str:
    return f"{node_type}:{name}"


def build_knowledge_graph(layer) -> KnowledgeGraph:
    """从语义层确定性构图（节点/边均排序，结果稳定可重放）。"""
    nodes: Dict[str, GraphNode] = {}
    edges: List[GraphEdge] = []

    def ensure(node: GraphNode) -> None:
        if node.id not in nodes:
            nodes[node.id] = node

    # ---- 模型 + 其维度/度量 ----
    for model in layer.manifest.models:
        ensure(
            GraphNode(
                id=_node_id(NODE_MODEL, model.name),
                type=NODE_MODEL,
                label=model.name,
                props={"table": model.table, "description": model.description},
            )
        )
        for dim in model.dimensions:
            ensure(
                GraphNode(
                    id=_node_id(NODE_DIMENSION, dim.name),
                    type=NODE_DIMENSION,
                    label=dim.name,
                    props={"type": dim.type, "description": dim.description},
                )
            )
            edges.append(
                GraphEdge(
                    source=_node_id(NODE_MODEL, model.name),
                    target=_node_id(NODE_DIMENSION, dim.name),
                    type=EDGE_HAS_DIMENSION,
                    label=EDGE_LABELS[EDGE_HAS_DIMENSION],
                )
            )
        for meas in model.measures:
            ensure(
                GraphNode(
                    id=_node_id(NODE_MEASURE, meas.name),
                    type=NODE_MEASURE,
                    label=meas.name,
                    props={"agg": meas.agg, "description": meas.description},
                )
            )
            edges.append(
                GraphEdge(
                    source=_node_id(NODE_MEASURE, meas.name),
                    target=_node_id(NODE_MODEL, model.name),
                    type=EDGE_OWNED_BY,
                    label=EDGE_LABELS[EDGE_OWNED_BY],
                )
            )

    # ---- 指标 + 关系 ----
    for metric in layer.list_metrics():
        ensure(
            GraphNode(
                id=_node_id(NODE_METRIC, metric.name),
                type=NODE_METRIC,
                label=metric.name,
                props={
                    "type": metric.type,
                    "unit": metric.unit,
                    "description": metric.description,
                    "aliases": metric.aliases,
                },
            )
        )
        for model in layer.metric_models(metric):
            edges.append(
                GraphEdge(
                    source=_node_id(NODE_METRIC, metric.name),
                    target=_node_id(NODE_MODEL, model.name),
                    type=EDGE_BASED_ON,
                    label=EDGE_LABELS[EDGE_BASED_ON],
                )
            )
        for meas_name in layer.base_measures(metric.name):
            edges.append(
                GraphEdge(
                    source=_node_id(NODE_METRIC, metric.name),
                    target=_node_id(NODE_MEASURE, meas_name),
                    type=EDGE_COMPUTED_FROM,
                    label=EDGE_LABELS[EDGE_COMPUTED_FROM],
                )
            )
        for ref in metric.input_metrics:
            edges.append(
                GraphEdge(
                    source=_node_id(NODE_METRIC, metric.name),
                    target=_node_id(NODE_METRIC, ref),
                    type=EDGE_DERIVED_FROM,
                    label=EDGE_LABELS[EDGE_DERIVED_FROM],
                )
            )

    # 排序保证确定性
    sorted_nodes = sorted(nodes.values(), key=lambda n: (n.type, n.id))
    sorted_edges = sorted(edges, key=lambda e: (e.source, e.type, e.target))
    return KnowledgeGraph(nodes=sorted_nodes, edges=sorted_edges)


def _graph_index(graph: KnowledgeGraph) -> Dict[str, GraphNode]:
    return {n.id: n for n in graph.nodes}


def subgraph_for_metrics(layer, metric_names: List[str], hops: int = 1) -> KnowledgeGraph:
    """围绕指定指标做 ``hops`` 跳子图（默认一跳邻居），用于聚焦展示。"""
    full = build_knowledge_graph(layer)
    index = _graph_index(full)
    keep: set = set()
    frontier = set(_node_id(NODE_METRIC, m) for m in metric_names if _node_id(NODE_METRIC, m) in index)

    if not frontier:
        # 指标名未命中：返回空图（前端据此提示"未收录"）
        return KnowledgeGraph(nodes=[], edges=[])

    for _ in range(max(0, hops)):
        next_frontier: set = set()
        for e in full.edges:
            if e.source in frontier and e.target not in keep:
                keep.add(e.target)
                next_frontier.add(e.target)
            if e.target in frontier and e.source not in keep:
                keep.add(e.source)
                next_frontier.add(e.source)
        frontier |= next_frontier
    keep |= {_node_id(NODE_METRIC, m) for m in metric_names}
    # 保留与 keep 相关的边
    sub_nodes = [n for n in full.nodes if n.id in keep]
    sub_edges = [e for e in full.edges if e.source in keep and e.target in keep]
    return KnowledgeGraph(nodes=sub_nodes, edges=sub_edges)


def extract_metric_names_from_query(layer, query: str) -> List[str]:
    """从自然语言问句中抽取命中的指标名（名称 / 别名子串匹配，去重保序）。

    用于检索增强：把命中的指标血缘展开成文本。子串匹配可能过匹配，但作为
    RAG 上下文增强的"召回候选"已足够；精确要素仍由主流程的意图识别给出。
    """
    if not query:
        return []
    hits: List[str] = []
    seen: set = set()
    q = query
    for metric in layer.list_metrics():
        candidates = [metric.name] + list(metric.aliases)
        for cand in candidates:
            if not cand:
                continue
            # 仅当候选作为连续片段出现才命中（避免 "存款" 误命中 "存款利息支出" 的尾段时，
            # 但若 "存款" 单独成词出现于问句也应命中——子串即可满足）
            if cand in q and cand not in seen:
                seen.add(cand)
                if metric.name not in hits:
                    hits.append(metric.name)
    return hits


def lineage_text(layer, metric_names: List[str]) -> str:
    """把指标血缘渲染为确定性文本（供检索增强注入 / 前端 tooltip）。"""
    if not metric_names:
        return ""
    lines: List[str] = []
    for name in metric_names:
        metric = layer.resolve_metric(name)
        if metric is None:
            continue
        models = [m.name for m in layer.metric_models(metric)]
        measures = layer.base_measures(name)
        dims = layer.supported_dimensions(metric)
        parts = [f"【{metric.name}】（{metric.type}）"]
        if metric.description:
            parts.append(f"说明：{metric.description}")
        if models:
            parts.append(f"基于数据域：{ '/'.join(models) }")
        if measures:
            parts.append(f"计算自度量：{ '/'.join(measures) }")
        refs = metric.input_metrics
        if refs:
            parts.append(f"派生自指标：{ '/'.join(refs) }")
        if dims:
            parts.append(f"可用维度：{ '/'.join(dims) }")
        lines.append("；".join(parts) + "。")
    return "\n".join(lines)
