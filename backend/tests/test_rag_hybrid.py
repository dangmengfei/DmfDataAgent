"""Phase 5 RAG 增强测试：BM25 检索 + 混合检索（RRF 融合）+ 黑话查询改写（无外部依赖）。"""

from langchain_core.documents import Document

from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import InMemoryChunkRepository, InMemoryDocumentRepository
from dmf_agent.rag.bm25 import BM25Index
from dmf_agent.rag.retriever import HybridKnowledgeRetriever
from dmf_agent.recall.black_talk import DEFAULT_CANT_DICT, CantDictionary
from dmf_agent.schemas.knowledge import ChunkData, DocumentData

CORPUS = [
    ("GMV 销售额的考核口径按季度统计，包含线上与线下渠道。", "指标管理制度"),
    ("大区销售目标与区域划分说明，华南大区负责华南区域。", "销售管理办法"),
    ("会员等级分为普通、白银、黄金、铂金。", "会员体系"),
]


def _make_bm25() -> BM25Index:
    return BM25Index(CORPUS)


async def _make_repos():
    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    doc = await doc_repo.add(DocumentData(id="d1", title="指标管理制度", source_type="text"))
    await chunk_repo.add_many(
        [
            ChunkData(id=f"c{i}", document_id=doc.id, chunk_index=i, content=content)
            for i, (content, _) in enumerate(CORPUS)
        ]
    )
    return doc_repo, chunk_repo


# ---- BM25 基础 ----


def test_bm25_relevant_doc_ranked_first():
    idx = _make_bm25()
    results = idx.search("销售额渠道", k=3)
    assert results and results[0]["content"].startswith("GMV 销售额")
    assert results[0]["bm25"] is True
    assert results[0]["source"] == "指标管理制度"


def test_bm25_chinese_and_english():
    idx = BM25Index([("GMV 达成率 weekly report", "周报")])
    results = idx.search("gmv weekly", k=3)
    assert results and "GMV" in results[0]["content"]


def test_bm25_empty_corpus():
    assert BM25Index([]).search("任何词", k=3) == []


def test_bm25_no_hit_empty():
    idx = _make_bm25()
    assert idx.search("完全不存在的词xyz", k=3) == []


def test_bm25_empty_query():
    assert _make_bm25().search("", k=3) == []


# ---- HybridKnowledgeRetriever ----


async def test_retrieve_bm25_only_without_vector():
    doc_repo, chunk_repo = await _make_repos()
    retriever = HybridKnowledgeRetriever(None, chunk_repo, doc_repo, Settings())
    results = await retriever.retrieve("销售额考核口径", k=3)
    assert results and "GMV" in results[0]["content"]
    assert results[0]["source"] == "指标管理制度"


async def test_retrieve_empty_corpus():
    retriever = HybridKnowledgeRetriever(
        None, InMemoryChunkRepository(), InMemoryDocumentRepository(), Settings()
    )
    assert await retriever.retrieve("任何词", k=3) == []


class _FakeVectorStore:
    def __init__(self, docs):
        self._docs = docs

    def search(self, query, k):
        return self._docs


async def test_retrieve_hybrid_rrf_fusion():
    doc_repo, chunk_repo = await _make_repos()
    # 向量命中"会员等级"文档，BM25 命中"销售额"文档 -> 融合后两者都在
    fake_store = _FakeVectorStore(
        [Document(page_content=CORPUS[2][0], metadata={"source": CORPUS[2][1], "score": 0.8})]
    )
    retriever = HybridKnowledgeRetriever(fake_store, chunk_repo, doc_repo, Settings())
    results = await retriever.retrieve("销售额 会员等级", k=3)
    contents = [r["content"] for r in results]
    assert any("会员等级" in c for c in contents)
    assert any("GMV 销售额" in c for c in contents)


async def test_expand_query_cant_hit():
    doc_repo, chunk_repo = await _make_repos()
    retriever = HybridKnowledgeRetriever(
        None,
        chunk_repo,
        doc_repo,
        Settings(),
        cant_dict=CantDictionary.from_raw_rows(DEFAULT_CANT_DICT),
    )
    expanded = retriever._expand_query("大区GMV")
    # 黑话"大区" -> 标准维度名"地区"与维值"华东/华南"追加到查询
    assert "地区" in expanded
    assert "华东" in expanded


async def test_expand_query_no_cant_dict():
    doc_repo, chunk_repo = await _make_repos()
    retriever = HybridKnowledgeRetriever(None, chunk_repo, doc_repo, Settings())
    assert retriever._expand_query("销售额") == "销售额"


# ---- 黑话改写提升 BM25 命中 ----


async def test_cant_rewrite_improves_bm25_hit():
    doc_repo, chunk_repo = await _make_repos()
    # 文档用标准维度名"地区"，用户用黑话"大区" -> 改写后能命中
    await chunk_repo.add_many(
        [
            ChunkData(
                id="c9",
                document_id="d1",
                chunk_index=9,
                content="地区划分与各级地区销售目标说明。",
            )
        ]
    )
    retriever = HybridKnowledgeRetriever(
        None,
        chunk_repo,
        doc_repo,
        Settings(),
        cant_dict=CantDictionary.from_raw_rows(DEFAULT_CANT_DICT),
    )
    # 仅用黑话"大区"查询：不改写时 bigram 命中不了"地区"文档
    results = await retriever.retrieve("大区", k=3)
    assert any("地区划分" in r["content"] for r in results)
