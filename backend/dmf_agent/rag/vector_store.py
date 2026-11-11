"""向量存储封装。

后端抽象为 VectorStore 协议：
- milvus : 默认，对齐老系统（HNSW/L2，M=32、efConstruction/ef=512）
  （MilvusVectorStore）。测试环境用 pymilvus 内嵌库（uri 指向本地文件
  如 data/milvus.db，免 Docker），生产指向 Milvus server。
- faiss   : 零依赖本地目录持久化（FaissVectorStore），需 pip install -e '.[rag]'
两种后端缺失依赖 / Embedding 未配置时均优雅降级（纯 BM25）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Protocol
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from dmf_agent.config.settings import Settings, get_settings
from dmf_agent.llm.providers import get_embeddings

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    def add(self, texts: List[str], metadatas: List[dict]) -> List[str]:
        """写入文本，返回 chunk id 列表。"""
        ...

    def search(self, query: str, k: int) -> List[Document]: ...

    def delete(self, ids: List[str]) -> None: ...

    def save(self) -> None: ...


class FaissVectorStore:
    """FAISS 本地向量库：目录持久化，进程内单例使用。"""

    def __init__(self, embeddings: Embeddings, persist_path: Path) -> None:
        self._embeddings = embeddings
        self._persist_path = persist_path
        self._store = None  # langchain_community FAISS 实例，惰性加载

        try:
            from langchain_community.vectorstores import FAISS
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "FAISS 依赖未安装，请执行: pip install -e '.[rag]'"
            ) from exc

        self._faiss_cls = FAISS
        persist_path.mkdir(parents=True, exist_ok=True)
        if any(persist_path.iterdir()):
            try:
                self._store = self._faiss_cls.load_local(
                    str(persist_path), embeddings, allow_dangerous_deserialization=True
                )
                logger.info("已加载本地向量库：%s", persist_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("本地向量库加载失败，将重建：%s", exc)

    def add(self, texts: List[str], metadatas: List[dict]) -> List[str]:
        if not texts:
            return []
        if self._store is None:
            self._store = self._faiss_cls.from_texts(
                texts, self._embeddings, metadatas=metadatas
            )
            # FAISS.from_texts 不返回 id；空库场景下 docstore 的键即本批 id
            ids = list(self._store.docstore._dict.keys())
        else:
            ids = self._store.add_texts(texts, metadatas=metadatas)
        self.save()
        return ids

    def search(self, query: str, k: int) -> List[Document]:
        if self._store is None:
            return []
        results = self._store.similarity_search_with_score(query, k=k)
        docs = []
        for doc, score in results:
            doc.metadata["score"] = float(score)
            docs.append(doc)
        return docs

    def delete(self, ids: List[str]) -> None:
        if self._store is not None and ids:
            self._store.delete(ids)
            self.save()

    def save(self) -> None:
        if self._store is not None:
            self._store.save_local(str(self._persist_path))


# Milvus HNSW 索引参数（对齐老系统 knowledge_service.import_contents_to_milvus）：
# L2 距离、HNSW（M=32、建库 efConstruction=512、检索 ef=512）。
_MILVUS_METRIC_TYPE = "L2"
_MILVUS_INDEX_TYPE = "HNSW"
_MILVUS_HNSW_M = 32
_MILVUS_HNSW_EF_CONSTRUCTION = 512
_MILVUS_HNSW_EF_SEARCH = 512


class MilvusVectorStore:
    """Milvus 向量库（pymilvus）。

    - 测试环境：uri 指向本地文件（pymilvus 内嵌库，免 Docker），如 data/milvus.db
    - 生产：uri 指向 Milvus server（http://host:19530），可带 token
    集合 schema：主键 id（VARCHAR，uuid）、向量列（FLOAT_VECTOR，维度惰性探测）、
    动态字段承载 text 与业务 metadata。向量列建 HNSW/L2 索引（参数对齐老系统）。
    save() 为 no-op（Milvus 自持久化）。
    """

    def __init__(
        self,
        embeddings: Embeddings,
        uri: str,
        token: str = "",
        collection_name: str = "dmf_agent_kb",
        grpc_options: Optional[dict] = None,
    ) -> None:
        self._embeddings = embeddings
        self._collection = collection_name
        self._dim: Optional[int] = None
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pymilvus 依赖未安装，请执行: pip install -e '.[milvus]'"
            ) from exc
        kwargs: dict = {"uri": uri}
        if token:
            kwargs["token"] = token
        # gRPC keepalive：pymilvus 默认 keepalive_time_ms=10000 + permit_without_calls=True，
        # 即空闲时每 10s 发一次 HTTP/2 PING。而 gRPC 服务端默认 min_recv_ping_interval=5min、
        # max_pings_without_data=2，累计 2 次"提前到达的 ping"就回 GOAWAY
        # ENHANCE_YOUR_CALM("too_many_pings") 并断开连接——日志里会周期性刷这行 E 错误。
        # 关掉"无调用也 ping"（有活跃调用时仍按 keepalive_time_ms 探活），空闲连接不再
        # 触发限流；Milvus 调用本身是短请求，无需靠空闲 ping 维持。调用方可经 grpc_options 覆盖。
        opts = {
            "grpc.keepalive_permit_without_calls": False,
            "grpc.keepalive_time_ms": 60000,
        }
        opts.update(grpc_options or {})
        kwargs["grpc_options"] = opts
        self._client = MilvusClient(**kwargs)

    def _ensure_collection(self) -> None:
        if self._client.has_collection(self._collection):
            return
        if self._dim is None:
            self._dim = len(self._embeddings.embed_query(""))
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(
            field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64
        )
        schema.add_field(
            field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._dim
        )
        # HNSW/L2 索引（M=32、efConstruction=512），对齐老系统
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type=_MILVUS_INDEX_TYPE,
            metric_type=_MILVUS_METRIC_TYPE,
            params={
                "M": _MILVUS_HNSW_M,
                "efConstruction": _MILVUS_HNSW_EF_CONSTRUCTION,
            },
        )
        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )
        logger.info(
            "已创建 Milvus 集合：%s（dim=%s, %s/%s）",
            self._collection,
            self._dim,
            _MILVUS_INDEX_TYPE,
            _MILVUS_METRIC_TYPE,
        )

    def add(self, texts: List[str], metadatas: List[dict]) -> List[str]:
        if not texts:
            return []
        vectors = self._embeddings.embed_documents(texts)
        self._ensure_collection()
        ids = [uuid4().hex for _ in texts]
        rows = []
        for i, text in enumerate(texts):
            row: dict = {"id": ids[i], "vector": vectors[i], "text": text}
            for k, v in (metadatas[i] if metadatas else {}).items():
                row[k] = v
            rows.append(row)
        self._client.insert(collection_name=self._collection, data=rows)
        return ids

    def search(self, query: str, k: int) -> List[Document]:
        if not self._client.has_collection(self._collection):
            return []
        vector = self._embeddings.embed_query(query)
        hits = self._client.search(
            collection_name=self._collection,
            data=[vector],
            limit=k,
            output_fields=["*"],
            search_params={
                "metric_type": _MILVUS_METRIC_TYPE,
                "params": {"ef": _MILVUS_HNSW_EF_SEARCH},
            },
        )
        docs = []
        for hit in (hits[0] if hits else []):
            entity = hit.get("entity") or {}
            metadata = {
                key: value
                for key, value in entity.items()
                if key not in ("text", "vector", "id")
            }
            doc = Document(page_content=entity.get("text", ""), metadata=metadata)
            doc.metadata["score"] = float(hit.get("distance", 0.0))
            docs.append(doc)
        return docs

    def delete(self, ids: List[str]) -> None:
        if ids and self._client.has_collection(self._collection):
            self._client.delete(collection_name=self._collection, ids=ids)

    def save(self) -> None:
        pass  # Milvus 数据自持久化


def build_vector_store(settings: Optional[Settings] = None) -> Optional[VectorStore]:
    """构建向量库（按后端 + 地址缓存进程内单例）；
    Embedding 未配置 / 依赖缺失时返回 None（RAG 降级）。"""
    s = settings or get_settings()
    embeddings = get_embeddings(s)
    if embeddings is None:
        return None

    if s.rag.backend == "milvus":
        cache_key = f"milvus::{s.rag.milvus_uri}::{s.rag.collection_name}"
        if cache_key in _VECTOR_INSTANCES:
            return _VECTOR_INSTANCES[cache_key]
        try:
            store = MilvusVectorStore(
                embeddings, s.rag.milvus_uri, s.rag.milvus_token, s.rag.collection_name
            )
        except RuntimeError as exc:
            logger.warning("Milvus 初始化失败，RAG 功能不可用：%s", exc)
            return None
        _VECTOR_INSTANCES[cache_key] = store
        return store

    persist_path = Path(s.rag.persist_dir) / "faiss"
    # 缓存 key 反映实际生效的 embedding 模型（本地模式用 embedding_local_model，
    # 避免切换本地模型后复用到旧 FAISS 索引）
    emb_id = (
        s.llm.embedding_local_model
        if s.llm.embedding_backend == "local"
        else s.llm.embedding_model
    )
    cache_key = f"{persist_path.resolve()}::{emb_id}"
    if cache_key in _VECTOR_INSTANCES:
        return _VECTOR_INSTANCES[cache_key]
    try:
        store = FaissVectorStore(embeddings, persist_path)
    except RuntimeError as exc:
        logger.warning("向量库初始化失败，RAG 功能不可用：%s", exc)
        return None
    _VECTOR_INSTANCES[cache_key] = store
    return store


# 进程内向量库实例缓存（同一后端 + 地址共享实例，
# 避免每次请求重新加载索引导致新入库文档检索不到）
_VECTOR_INSTANCES: dict = {}
