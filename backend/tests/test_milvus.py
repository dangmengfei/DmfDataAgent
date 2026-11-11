"""Phase: Milvus 向量库测试（pymilvus 内嵌模式，免 Docker）。

pymilvus 未安装时跳过（pytest.importorskip），不影响无 milvus 依赖的 CI。
"""

import os

import pytest

pymilvus = pytest.importorskip("pymilvus")

from dmf_agent.config.settings import Settings  # noqa: E402
from dmf_agent.rag.vector_store import MilvusVectorStore  # noqa: E402


class _FakeEmbeddings:
    """固定 4 维确定性伪 embedding（用不到真实模型）。"""

    dim = 4

    def _vec(self, s: str):
        seed = sum(ord(c) for c in s)
        return [float((seed + i * 7) % 100) / 100 for i in range(self.dim)]

    def embed_query(self, text: str):
        return self._vec(text)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


def _store(tmp_path) -> MilvusVectorStore:
    uri = os.path.join(str(tmp_path), "milvus.db")
    return MilvusVectorStore(_FakeEmbeddings(), uri, collection_name="test_kb")


def test_add_and_search_roundtrip(tmp_path):
    store = _store(tmp_path)
    ids = store.add(["GMV 达成率说明", "会员等级体系"], [{"doc_id": "d1"}, {"doc_id": "d2"}])
    assert len(ids) == 2

    docs = store.search("GMV 达成率", k=2)
    assert docs
    # 假 embedding 排序无业务意义，断言机制：命中集合 + metadata/score 透出
    assert any("GMV" in d.page_content for d in docs)
    assert any(d.metadata.get("doc_id") == "d1" for d in docs)
    assert all("score" in d.metadata for d in docs)


def test_search_empty_collection_returns_empty(tmp_path):
    store = _store(tmp_path)
    assert store.search("任何词", k=3) == []


def test_delete_removes_docs(tmp_path):
    store = _store(tmp_path)
    ids = store.add(["唯一内容 XYZ"], [{}])
    assert store.search("XYZ", k=3)
    store.delete(ids)
    # 删除后应检索不到
    assert store.search("XYZ", k=3) == []


def test_save_is_noop(tmp_path):
    store = _store(tmp_path)
    store.save()  # Milvus 自持久化，save 不抛错


def test_build_vector_store_milvus_backend(monkeypatch, tmp_path):
    from dmf_agent.rag import vector_store as vs

    uri = os.path.join(str(tmp_path), "milvus.db")
    settings = Settings(rag=vs_rag_settings(uri))
    monkeypatch.setattr(vs, "get_embeddings", lambda s: _FakeEmbeddings())
    store = vs.build_vector_store(settings)
    assert isinstance(store, MilvusVectorStore)
    # 进程内缓存命中
    assert vs.build_vector_store(settings) is store


def vs_rag_settings(uri):
    from dmf_agent.config.settings import RAGSettings

    return RAGSettings(backend="milvus", milvus_uri=uri)
