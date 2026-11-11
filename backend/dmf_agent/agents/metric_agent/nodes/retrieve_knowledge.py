"""知识检索节点（RAG）。

迁移自老系统 metadata/sa_meta/knowledge/knowledge_service.py
（BM25 + 向量混合检索 + 黑话词典），当前为向量检索，
TODO: 融合 BM25（plugin/utils/bm25_recall.py）与黑话词典（CantFuncs）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, user_query

logger = logging.getLogger(__name__)


def _augment_with_lineage(deps: AgentDeps, query: str, docs: List[Any]) -> List[Any]:
    """检索增强（Task D）：把问句命中的指标血缘展开成一条合成文档注入检索结果。

    纯增量、零副作用：任何异常都只告警并原样返回 ``docs``，绝不阻断主检索链路。
    """
    layer = getattr(deps, "semantic_layer", None)
    if layer is None:
        return docs
    try:
        from dmf_agent.rag.knowledge_graph import (
            extract_metric_names_from_query,
            lineage_text,
        )

        metrics = extract_metric_names_from_query(layer, query)
        if not metrics:
            return docs
        text = lineage_text(layer, metrics)
        if not text:
            return docs
        lineage_doc = {
            "id": "kg:lineage:" + "|".join(metrics),
            "title": f"指标血缘（{'/'.join(metrics)}）",
            "content": text,
            "source_type": "knowledge_graph",
            "authority_level": "authoritative",
            "score": 1.0,
            "lineage_metrics": metrics,
        }
        # 血缘为结构化权威常识，置于最前，优先被下游消费
        return [lineage_doc] + list(docs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标血缘检索增强失败（跳过）：%s", exc)
        return docs


def build_retrieve_knowledge_node(deps: AgentDeps):
    async def retrieve_knowledge(state: AgentState) -> Dict[str, Any]:
        if deps.retriever is None:
            logger.info("知识检索器不可用（RAG 未配置），返回空结果")
            return {"retrieved_docs": [], "mock_mode": True}
        # 多租户隔离：带上当前租户，只检索本租户文档（空串按"不隔离"处理，
        # 与鉴权关闭时的既有行为一致）
        # 个人/企业隔离：带上当前用户 id，个人/私有库文档仅本人可见（防同租户互见）
        # 查询用**改写后**的规范问句：黑话已翻译为标准术语，BM25/向量命中率显著提升
        tenant_id = state.get("tenant_id") or None
        owner_id = state.get("user_id") or None
        try:
            docs = await deps.retriever.retrieve(
                user_query(state), tenant_id=tenant_id, owner_id=owner_id
            )
        except TypeError:
            # 自定义检索器实现未跟上租户参数：降级为不隔离调用
            docs = await deps.retriever.retrieve(user_query(state))
        # 检索增强：注入命中指标的血缘常识（不计入"命中条数"日志口径，仅作上下文）
        docs = _augment_with_lineage(deps, user_query(state), docs)
        logger.info("知识检索命中 %s 条（tenant=%s）", len(docs), tenant_id or "-")
        return {"retrieved_docs": docs}

    return retrieve_knowledge
