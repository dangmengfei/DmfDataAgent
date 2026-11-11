"""语义血缘知识图谱（Task D）测试。

用内联最小化清单构造语义层，避免依赖真实银行零售清单的具体内容，保证确定性。
"""

from dmf_agent.rag import knowledge_graph as kg
from dmf_agent.semantic import SemanticLayer

_RAW = {
    "semantic_models": [
        {
            "name": "m_deposit",
            "table": "db.t_deposit",
            "description": "存款域",
            "dimensions": [
                {"name": "region", "type": "categorical", "expr": "region"},
                {"name": "dt", "type": "time", "expr": "dt"},
            ],
            "measures": [
                {"name": "m_balance", "agg": "sum", "expr": "balance"},
            ],
        }
    ],
    "metrics": [
        {
            "name": "deposit_balance",
            "type": "simple",
            "type_params": {"measure": "m_balance"},
            "aliases": ["存款余额"],
            "description": "存款余额",
        }
    ],
}


def _layer():
    return SemanticLayer.from_manifest_dict(_RAW)


def test_build_knowledge_graph_nodes_and_edges():
    g = kg.build_knowledge_graph(_layer())
    types = {n.type for n in g.nodes}
    assert types == {"metric", "dimension", "model", "measure"}, types
    assert len(g.nodes) == 5  # 1 指标 + 2 维度 + 1 模型 + 1 度量

    edge_pairs = {(e.source, e.type, e.target) for e in g.edges}
    assert ("model:m_deposit", "has_dimension", "dimension:region") in edge_pairs
    assert ("model:m_deposit", "has_dimension", "dimension:dt") in edge_pairs
    assert ("measure:m_balance", "owned_by", "model:m_deposit") in edge_pairs
    assert ("metric:deposit_balance", "based_on", "model:m_deposit") in edge_pairs
    assert ("metric:deposit_balance", "computed_from", "measure:m_balance") in edge_pairs


def test_build_knowledge_graph_is_deterministic():
    a = kg.build_knowledge_graph(_layer())
    b = kg.build_knowledge_graph(_layer())
    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]
    assert [(e.source, e.type, e.target) for e in a.edges] == [
        (e.source, e.type, e.target) for e in b.edges
    ]


def test_subgraph_for_metrics_focus():
    g = kg.subgraph_for_metrics(_layer(), ["deposit_balance"], hops=1)
    ids = {n.id for n in g.nodes}
    # 聚焦指标 + 一跳邻居（模型、度量）；维度不直接连指标，不在子图
    assert "metric:deposit_balance" in ids
    assert "model:m_deposit" in ids
    assert "measure:m_balance" in ids
    assert "dimension:region" not in ids
    # 子图边只保留与 keep 集合相关的
    assert all(e.source in ids and e.target in ids for e in g.edges)


def test_subgraph_unknown_metric_returns_empty():
    g = kg.subgraph_for_metrics(_layer(), ["not_exist"], hops=1)
    assert g.nodes == []
    assert g.edges == []


def test_extract_metric_names_from_query_by_alias():
    layer = _layer()
    hits = kg.extract_metric_names_from_query(layer, "帮我看下存款余额是多少")
    assert hits == ["deposit_balance"]


def test_lineage_text_contains_lineage():
    layer = _layer()
    text = kg.lineage_text(layer, ["deposit_balance"])
    assert "【deposit_balance】" in text
    assert "基于数据域：m_deposit" in text
    assert "计算自度量：m_balance" in text


def test_lineage_text_empty_for_unknown():
    layer = _layer()
    assert kg.lineage_text(layer, ["nope"]) == ""
