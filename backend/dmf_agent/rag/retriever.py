"""知识检索器：BM25 + 向量混合检索（供 Agent 图的 retrieve_knowledge 节点调用）。

迁移自老系统 metadata/sa_meta/knowledge/knowledge_service.py（BM25 + 向量混合检索）：
- BM25（rag/bm25.py，纯 Python）：Embedding 未配置时知识问答仍可用（离线降级）
- 向量（VectorStore.search）：Embedding 配置后与 BM25 做 RRF 融合（rank 融合，无需分数归一）
- 语料来自 chunk 仓储（DB 落库的分块文本），源标题经 document 仓储映射
- **租户隔离**：`retrieve(..., tenant_id=...)` 只检索该租户文档的分块；
  BM25 语料按租户分别建索引并缓存，向量结果按 doc_id 过滤（None = 不隔离，兼容旧行为）。

FR7 检索两阶段（"先召回、再精排"）：
1. **召回**：BM25 + 向量各自取 `candidate_k` 条 -> RRF 融合（追求"不漏"）
2. **精排**：cross-encoder 重排（`rag/reranker.py`，追求"排序准"）；服务不可用时
   **优雅降级**为 RRF 顺序，知识问答不受影响
3. **分级加权**：按文档来源分级（authoritative 权威源 / sediment 沉淀源 / user 上传）
   对分数加权，权威口径文档优先顶到前面
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, Set

from dmf_agent.config.settings import Settings
from dmf_agent.db.base import ChunkRepository, DocumentRepository
from dmf_agent.rag.bm25 import BM25Index
from dmf_agent.rag.reranker import RerankerClient
from dmf_agent.rag.vector_store import VectorStore

logger = logging.getLogger(__name__)

# RRF 融合常数（rank 融合标准 k=60，老系统与常见做法一致）
_RRF_K = 60


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        query: str,
        k: Optional[int] = None,
        tenant_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        rerank: Optional[bool] = None,
    ) -> List[dict]: ...


class HybridKnowledgeRetriever:
    def __init__(
        self,
        vector_store: Optional[VectorStore],
        chunk_repo: ChunkRepository,
        document_repo: DocumentRepository,
        settings: Settings,
        cant_dict=None,
        reranker: Optional[RerankerClient] = None,
        kb_repo=None,
    ) -> None:
        self._store = vector_store
        self._chunk_repo = chunk_repo
        self._document_repo = document_repo
        self._settings = settings
        self._cant_dict = cant_dict  # 黑话词典（查询改写用，Phase 2.4b 产出）
        # 重排客户端（FR7 第二阶段）；None = 不重排（配置关闭 / 服务不可用）
        self._reranker = reranker
        # 知识库仓储（检索隔离用：查文档归属 KB 的 type/visibility 决定可见性）
        self._kb_repo = kb_repo
        # 惰性构建的 BM25 索引：按 (租户, owner) 维度缓存（"" 表示不隔离）
        self._bm25_cache: Dict[str, Optional[BM25Index]] = {}
        # 文档 id -> 租户（构建全量语料时顺手记录）
        self._doc_tenant: Dict[str, str] = {}
        # 文档 id -> 归属用户（检索隔离：个人/私有文档仅 owner 可见）
        self._doc_owner: Dict[str, str] = {}
        # 文档 id -> 归属知识库 id（判断企业/公开库全员可见）
        self._doc_kb: Dict[str, str] = {}
        # 知识库 id -> (type, visibility)（判断企业/公开库全员可见）
        self._base_info: Dict[str, tuple] = {}
        # 文档 id -> {title, authority_level}（来源分级加权用）
        self._doc_meta: Dict[str, Dict[str, str]] = {}
        # 分块内容 -> 文档 id（BM25 结果回填文档级元数据；向量路径自带 doc_id）
        self._content_doc: Dict[str, str] = {}

    def _expand_query(self, query: str) -> str:
        """黑话查询改写：命中黑话维度/维值时追加标准名，提升 BM25/向量命中率。

        迁移自老系统知识检索对 CantFuncs 的利用（黑话 -> 标准维度名）。
        """
        if self._cant_dict is None or not query.strip():
            return query
        cant = self._cant_dict.recall(query)
        extras: list = []
        extras.extend(cant.dimensions)
        extras.extend(cant.dimensions_cant)
        for f in cant.filters:
            extras.append(f.get("dimension"))
            extras.extend(f.get("dimension_value") or [])
        extras = [e for e in extras if e and e not in query]
        return f"{query} {' '.join(extras)}" if extras else query

    @staticmethod
    def _cache_key(tenant_id: Optional[str], owner_id: Optional[str]) -> str:
        return f"{tenant_id if tenant_id is not None else ''}|{owner_id if owner_id is not None else ''}"

    async def _ensure_bm25(
        self, tenant_id: Optional[str] = None, owner_id: Optional[str] = None
    ) -> None:
        """按 (租户, owner) 惰性构建 BM25 语料。

        隔离关闭（tenant_id 为 None）时语料为全量，与改造前行为一致；
        隔离开启时只纳入「租户匹配且对该用户可见」的文档分块。
        可见性规则：企业库 / 公开库 / owner 为空（系统权威源）的同租户文档对全员可见；
        其余个人/私有文档仅 owner 本人可见（防同租户 A 的个人库被 B 检索到）。
        """
        key = self._cache_key(tenant_id, owner_id)
        if key in self._bm25_cache:
            return
        chunks = await self._chunk_repo.list_all()
        docs = await self._document_repo.list()  # tenant_id 默认不过滤：全量，用于建映射
        title_map = {d.id: d.title for d in docs}
        # 预载知识库 (type, visibility)，仅在有 kb_repo 时（避免无谓查询）
        if self._kb_repo is not None and not self._base_info:
            try:
                bases = await self._kb_repo.list()
                self._base_info = {
                    b.id: (b.type, b.visibility) for b in bases
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载知识库可见性失败（按保守口径隔离）：%s", exc)
        for d in docs:
            self._doc_tenant[d.id] = d.tenant_id or ""
            self._doc_owner[d.id] = d.owner_id or ""
            self._doc_kb[d.id] = d.knowledge_base_id or ""
            # FR7：来源分级随文档缓存，供融合后加权（无需额外查询）
            self._doc_meta[d.id] = {
                "title": d.title,
                "authority_level": d.authority_level or "user",
            }
        visible = self._visible_doc_ids(tenant_id, owner_id)
        if visible is not None:
            chunks = [c for c in chunks if c.document_id in visible]
        if not chunks:
            self._bm25_cache[key] = None
            return
        for c in chunks:
            self._content_doc[c.content] = c.document_id
        corpus = [(c.content, title_map.get(c.document_id, c.document_id)) for c in chunks]
        self._bm25_cache[key] = BM25Index(corpus)

    def _doc_shared_within_tenant(self, doc_id: str) -> bool:
        """文档是否对同租户全员可见（无需 owner 匹配）。

        企业库 / 公开库 或 owner 为空（系统权威源）视为全员可见；
        个人/私有库且 owner 非空则需 owner 本人匹配。
        """
        if (self._doc_owner.get(doc_id, "") or "") == "":
            return True
        kb_id = self._doc_kb.get(doc_id)
        if kb_id and kb_id in self._base_info:
            btype, bvis = self._base_info[kb_id]
            if btype == "enterprise" or bvis == "public":
                return True
        return False

    def _visible_doc_ids(
        self, tenant_id: Optional[str], owner_id: Optional[str]
    ) -> Optional[Set[str]]:
        """该 (租户, owner) 可见文档 id 集合；None 表示不隔离（全量）。"""
        if tenant_id is None:
            return None
        visible: Set[str] = set()
        for doc_id, doc_tenant in self._doc_tenant.items():
            if doc_tenant != tenant_id:
                continue
            if self._doc_shared_within_tenant(doc_id):
                visible.add(doc_id)
            elif owner_id is not None and self._doc_owner.get(doc_id, "") == owner_id:
                visible.add(doc_id)
        return visible

    def _doc_of(self, content: str, doc_id: Optional[str] = None) -> Optional[str]:
        """定位分块所属文档 id：向量路径自带，BM25 路径由内容反查。"""
        return doc_id or self._content_doc.get(content)

    def _authority_of(self, content: str, doc_id: Optional[str] = None) -> str:
        """分块所属文档的来源分级；未知一律按 user（不加权、不误升权）。"""
        resolved = self._doc_of(content, doc_id)
        meta = self._doc_meta.get(resolved or "")
        return (meta or {}).get("authority_level") or "user"

    def _authority_weight(self, level: str) -> float:
        weights = self._settings.rag.authority_weights or {}
        try:
            return float(weights.get(level, 1.0))
        except (TypeError, ValueError):
            return 1.0

    async def retrieve(
        self,
        query: str,
        k: Optional[int] = None,
        tenant_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        rerank: Optional[bool] = None,
    ) -> List[dict]:
        top_k = k or self._settings.rag.top_k
        # 是否启用重排：调用方显式指定优先；未指定按配置；无客户端则强制关闭
        use_rerank = (
            self._settings.rag.rerank_enabled if rerank is None else bool(rerank)
        ) and self._reranker is not None
        # 重排需要更宽的候选池（召回全 -> 精排准），否则重排无从挑选
        candidate_k = max(top_k, self._settings.rag.rerank_top_n) if use_rerank else top_k

        await self._ensure_bm25(tenant_id, owner_id)
        bm25 = self._bm25_cache.get(self._cache_key(tenant_id, owner_id))
        expanded = self._expand_query(query)
        bm25_docs = bm25.search(expanded, candidate_k) if bm25 else []

        if self._store is None:
            # 无 Embedding：纯 BM25 检索（知识问答离线可用）
            return await self._rank(self._as_items(bm25_docs), expanded, top_k, use_rerank)

        try:
            vector_docs = self._store.search(expanded, candidate_k)
        except Exception as exc:  # noqa: BLE001 - 向量检索失败降级 BM25
            logger.warning("向量检索失败，降级 BM25：%s", exc)
            vector_docs = []
        # 检索隔离：向量库是全局的，按 doc_id 过滤掉「非本租户」或「非本人可见」文档
        visible = self._visible_doc_ids(tenant_id, owner_id)
        if visible is not None and vector_docs:
            vector_docs = [
                d for d in vector_docs
                if (d.metadata or {}).get("doc_id") in visible
            ]
        items = self._fuse(vector_docs, bm25_docs, candidate_k)
        return await self._rank(items, expanded, top_k, use_rerank)

    def _as_items(self, bm25_docs: List[dict]) -> List[dict]:
        """纯 BM25 结果 -> 统一条目结构（补 doc_id / 分级 / 原始分数）。"""
        items = []
        for item in bm25_docs:
            content = item["content"]
            level = self._authority_of(content)
            items.append(
                {
                    "content": content,
                    "source": item.get("source", ""),
                    "doc_id": self._doc_of(content),
                    "authority_level": level,
                    "rrf_score": float(item.get("score", 0.0)),
                    "score": round(float(item.get("score", 0.0)) * self._authority_weight(level), 6),
                }
            )
        return items

    def _fuse(self, vector_docs: List, bm25_docs: List[dict], top_k: int) -> List[dict]:
        """RRF 融合：两个检索系统按名次 1/(k+rank) 贡献分数。

        输出条目统一带 `doc_id` / `authority_level`（供重排与分级加权使用），
        `score` 为**已乘分级权重**的最终排序分。
        """
        rrf: dict[str, float] = {}
        for i, doc in enumerate(vector_docs):
            rrf[doc.page_content] = rrf.get(doc.page_content, 0.0) + 1.0 / (_RRF_K + i + 1)
        for i, item in enumerate(bm25_docs):
            rrf[item["content"]] = rrf.get(item["content"], 0.0) + 1.0 / (_RRF_K + i + 1)

        bm25_by_content = {item["content"]: item for item in bm25_docs}
        vector_by_content = {doc.page_content: doc for doc in vector_docs}
        results = []
        for content, score in sorted(rrf.items(), key=lambda kv: -kv[1])[:top_k]:
            source = ""
            doc_id = None
            if content in vector_by_content:
                meta = vector_by_content[content].metadata or {}
                source = meta.get("source", "")
                doc_id = meta.get("doc_id")
            elif content in bm25_by_content:
                source = bm25_by_content[content]["source"]
            level = self._authority_of(content, doc_id)
            results.append(
                {
                    "content": content,
                    "source": source,
                    "doc_id": self._doc_of(content, doc_id),
                    "authority_level": level,
                    "rrf_score": round(score, 6),
                    "score": round(score * self._authority_weight(level), 6),
                }
            )
        return results

    async def _rank(
        self, items: List[dict], query: str, top_k: int, use_rerank: bool
    ) -> List[dict]:
        """重排 + 分级加权 + 截断（返回最终 top_k）。

        重排失败（返回 None）时**保持 RRF 顺序**，整条链路不受影响。
        """
        if not items:
            return []
        if use_rerank:
            ranked = await self._reranker.rerank(query, [it["content"] for it in items])
            if ranked:
                # 重排返回 (原始下标, 相关性 0~1)：相关性替换融合分，再乘分级权重
                order = {idx: score for idx, score in ranked}
                for idx, item in enumerate(items):
                    if idx not in order:
                        continue
                    item["rerank_score"] = round(order[idx], 6)
                    item["reranked"] = True
                    item["score"] = round(
                        order[idx] * self._authority_weight(item["authority_level"]), 6
                    )
        items.sort(key=lambda it: -it["score"])
        return items[:top_k]
