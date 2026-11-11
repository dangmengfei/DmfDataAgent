"""FR7 知识库增强测试：来源分级 + cross-encoder 重排 + 权威源（口径字典）灌入。

覆盖三层：
1. 口径字典文本生成（内容 100% 来自语义层，公式/别名/维度齐全）
2. 灌入幂等（同一 source_uri 覆盖更新，不产生重复文档）
3. 重排客户端与检索链路（成功重排 / 各类失败优雅降级 / 分级加权）

无外部依赖：重排服务用假 httpx 客户端替代，向量库用假实现。
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import pytest

from dmf_agent.config.settings import LLMSettings, RAGSettings, Settings
from dmf_agent.db.memory import InMemoryChunkRepository, InMemoryDocumentRepository
from dmf_agent.rag.dictionary import (
    DICTIONARY_SOURCE_URI,
    DICTIONARY_TITLE,
    build_dictionary_text,
    seed_dictionary,
)
from dmf_agent.rag.reranker import RerankerClient, build_reranker
from dmf_agent.rag.retriever import HybridKnowledgeRetriever
from dmf_agent.schemas.knowledge import ChunkData, DocumentData
from dmf_agent.semantic.layer import SemanticLayer


# ==================== 口径字典：文本生成 ====================


def test_dictionary_covers_all_metrics():
    layer = SemanticLayer()
    text = build_dictionary_text(layer)
    expected = layer.metric_names()
    assert len(expected) == 22, f"语义层指标数变化，测试需同步：{len(expected)}"
    for name in expected:
        assert f"| {name} |" in text or f"### {name}" in text, f"字典缺少指标 {name}"


def test_dictionary_has_deterministic_formulas():
    """口径必须来自语义层的真实表达式，而不是通用占位文案。"""
    text = build_dictionary_text(SemanticLayer())
    # simple：measure 的聚合 + 物理列
    assert "SUM(gmv)" in text
    # ratio：分子 / 分母（保留中文指标名，便于业务核对）
    assert "零售贷款余额 / 零售存款余额" in text
    # derived：清单里的表达式原文
    assert "GMV / 订单数" in text
    assert "贷款利息收入 - 存款利息支出" in text


def test_dictionary_declares_authority_and_aliases():
    text = build_dictionary_text(SemanticLayer())
    assert "唯一权威说明" in text, "字典必须声明自己是权威源"
    assert "以本文件为准" in text
    # 口语别名要落到标准名（检索靠这些词命中）
    assert "DAU" in text and "日活跃用户数" in text
    assert "不良率" in text


def test_dictionary_lists_dimensions_and_models():
    text = build_dictionary_text(SemanticLayer())
    for dim in ("渠道", "门店", "机构", "客户等级"):
        assert dim in text
    # 底表溯源（问"数据从哪来"时命中）
    assert "dws_trade_day" in text and "dws_retail_deposit_day" in text
    # 口径红线
    assert "存款积数" in text


# ==================== 口径字典：灌入幂等 ====================


async def test_seed_dictionary_ingests_as_authoritative():
    doc_repo, chunk_repo = InMemoryDocumentRepository(), InMemoryChunkRepository()
    result = await seed_dictionary(
        document_repo=doc_repo,
        chunk_repo=chunk_repo,
        vector_store=None,  # 无嵌入服务：仅落文本分块（BM25 仍可检索）
        settings=Settings(),
        layer=SemanticLayer(),
    )
    assert result["authority_level"] == "authoritative"
    assert result["chunks"] > 0
    assert result["replaced"] is False
    doc = await doc_repo.get(result["document_id"])
    assert doc is not None and doc.title == DICTIONARY_TITLE
    assert doc.source_uri == DICTIONARY_SOURCE_URI
    assert doc.status == "indexed"
    # 分级统计能反映出来（前端据此提示"权威源是否已灌"）
    assert (await doc_repo.count_by_authority()).get("authoritative") == 1


async def test_seed_dictionary_is_idempotent():
    """重复灌入 = 覆盖更新：文档数不增长，旧分块被清掉。"""
    doc_repo, chunk_repo = InMemoryDocumentRepository(), InMemoryChunkRepository()
    first = await seed_dictionary(
        document_repo=doc_repo,
        chunk_repo=chunk_repo,
        vector_store=None,
        settings=Settings(),
        layer=SemanticLayer(),
    )
    first_chunks = len(await chunk_repo.list_by_document(first["document_id"]))

    second = await seed_dictionary(
        document_repo=doc_repo,
        chunk_repo=chunk_repo,
        vector_store=None,
        settings=Settings(),
        layer=SemanticLayer(),
    )
    assert second["replaced"] is True, "第二次灌入应识别并清理旧文档"
    docs = await doc_repo.list(source_uri=DICTIONARY_SOURCE_URI)
    assert len(docs) == 1, f"幂等失败：出现 {len(docs)} 份口径字典"
    # 旧文档与其分块都已清理，不残留
    assert await doc_repo.get(first["document_id"]) is None
    assert await chunk_repo.list_by_document(first["document_id"]) == []
    assert len(await chunk_repo.list_by_document(second["document_id"])) == first_chunks


# ==================== 重排客户端：成功路径 ====================


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload or "")

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    """替换 httpx.AsyncClient：按构造时给的响应/异常行为返回。"""

    response: Optional[_FakeResponse] = None
    exc: Optional[Exception] = None
    captured: dict = {}

    def __init__(self, **kwargs):
        self._timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).captured = {"url": url, "json": json, "headers": headers}
        if type(self).exc is not None:
            raise type(self).exc
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    def _install(response=None, exc=None):
        _FakeAsyncClient.response = response
        _FakeAsyncClient.exc = exc
        _FakeAsyncClient.captured = {}
        monkeypatch.setattr(
            "dmf_agent.rag.reranker.httpx.AsyncClient", _FakeAsyncClient
        )
        return _FakeAsyncClient

    return _install


async def test_reranker_parses_success(fake_http):
    """Cohere/Jina 风格响应 -> [(原始下标, 分数)]，保持服务返回的顺序。"""
    fake_http(
        response=_FakeResponse(
            200,
            {
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.42},
                ]
            },
        )
    )
    client = RerankerClient(base_url="http://rerank:8001/v1")
    out = await client.rerank("存款口径", ["A 文档", "B 文档", "C 文档"])
    assert out == [(2, 0.91), (0, 0.42)]
    assert client.available is True
    # 契约：query + documents 一次性批量送出，模型名带上
    assert _FakeAsyncClient.captured["json"]["query"] == "存款口径"
    assert _FakeAsyncClient.captured["json"]["documents"] == ["A 文档", "B 文档", "C 文档"]
    assert _FakeAsyncClient.captured["url"].endswith("/rerank")


async def test_reranker_empty_input_no_call(fake_http):
    fake_http(response=_FakeResponse(200, {"results": []}))
    client = RerankerClient(base_url="http://rerank:8001/v1")
    assert await client.rerank("问题", []) == []
    assert await client.rerank("   ", ["A"]) == []
    # 不必要地打网络（空请求不应触发调用）
    assert _FakeAsyncClient.captured == {}


# ==================== 重排客户端：优雅降级 ====================


async def test_reranker_non_200_degrades(fake_http):
    """503 = 服务在但模型未就位（可识别）：返回 None，调用方保持 RRF 顺序。"""
    fake_http(response=_FakeResponse(503, text="重排模型加载失败"))
    client = RerankerClient(base_url="http://rerank:8001/v1")
    assert await client.rerank("问题", ["A", "B"]) is None
    assert client.available is False


async def test_reranker_connection_error_degrades(fake_http):
    fake_http(exc=RuntimeError("connection refused"))
    client = RerankerClient(base_url="http://rerank:8001/v1")
    assert await client.rerank("问题", ["A", "B"]) is None
    assert client.available is False


async def test_reranker_malformed_response_degrades(fake_http):
    """响应缺 results / 字段类型不对 -> 降级，不抛异常到主链路。"""
    fake_http(response=_FakeResponse(200, {"unexpected": "shape"}))
    client = RerankerClient(base_url="http://rerank:8001/v1")
    assert await client.rerank("问题", ["A"]) is None

    # 下标越界与非法分数的条目被丢弃，合法条目保留
    fake_http(
        response=_FakeResponse(
            200,
            {
                "results": [
                    {"index": 99, "relevance_score": 0.9},  # 越界
                    {"index": "x", "relevance_score": 0.5},  # 非法下标
                    {"index": 1, "relevance_score": 0.7},  # 合法
                ]
            },
        )
    )
    assert await client.rerank("问题", ["A", "B"]) == [(1, 0.7)]


async def test_reranker_probe(fake_http):
    fake_http(response=_FakeResponse(200, {"results": [{"index": 0, "relevance_score": 0.5}]}))
    client = RerankerClient(base_url="http://rerank:8001/v1")
    assert await client.probe() is True

    fake_http(exc=RuntimeError("boom"))
    assert await client.probe() is False


# ==================== build_reranker 配置接线 ====================


def test_build_reranker_disabled_returns_none():
    s = Settings(rag=RAGSettings(rerank_enabled=False))
    assert build_reranker(s) is None


def test_build_reranker_defaults_to_embedding_service():
    """未显式配地址 -> 复用嵌入服务地址（两者同源，少一处配置）。"""
    s = Settings(
        rag=RAGSettings(rerank_enabled=True, rerank_base_url=""),
        llm=LLMSettings(embedding_base_url="http://embed:8001/v1"),
    )
    client = build_reranker(s)
    assert client is not None
    assert client.endpoint == "http://embed:8001/v1/rerank"


def test_build_reranker_no_address_returns_none():
    s = Settings(
        rag=RAGSettings(rerank_enabled=True, rerank_base_url=""),
        llm=LLMSettings(embedding_base_url=""),
    )
    assert build_reranker(s) is None


def test_build_reranker_explicit_url_wins():
    s = Settings(
        rag=RAGSettings(rerank_enabled=True, rerank_base_url="http://commercial/v2/"),
        llm=LLMSettings(embedding_base_url="http://embed:8001/v1"),
    )
    client = build_reranker(s)
    assert client is not None
    assert client.endpoint == "http://commercial/v2/rerank", "应去掉重复斜杠"


# ==================== 检索链路：重排 + 分级加权 ====================


class _StubReranker:
    """可控重排器：按 index->score 打分（模拟 cross-encoder 语义排序）。"""

    def __init__(self, scores: dict, fail: bool = False):
        self._scores = scores
        self._fail = fail
        self.calls: List[Tuple[str, List[str]]] = []

    async def rerank(self, query, documents, top_n=None):
        self.calls.append((query, list(documents)))
        if self._fail:
            return None
        return sorted(
            ((i, self._scores.get(i, 0.0)) for i in range(len(documents))),
            key=lambda kv: -kv[1],
        )


async def _retriever_with(docs, *, reranker=None, settings=None):
    """构造检索器：BM25 索引三篇文档，可注入假重排器。"""
    doc_repo, chunk_repo = InMemoryDocumentRepository(), InMemoryChunkRepository()
    for d in docs:
        await doc_repo.add(
            DocumentData(
                id=d["id"],
                title=d["title"],
                source_type="text",
                authority_level=d.get("authority_level", "user"),
            )
        )
        await chunk_repo.add_many(
            [
                ChunkData(
                    id=f"{d['id']}-c",
                    document_id=d["id"],
                    chunk_index=0,
                    content=d["content"],
                )
            ]
        )
    s = settings or Settings(rag=RAGSettings(rerank_top_n=10))
    return HybridKnowledgeRetriever(None, chunk_repo, doc_repo, s, reranker=reranker)


_DOCS = [
    {
        "id": "d1",
        "title": "指标管理制度",
        "content": "GMV 销售额的考核口径按季度统计，包含线上与线下渠道。",
    },
    {
        "id": "d2",
        "title": "销售管理办法",
        "content": "GMV 销售额目标与区域划分说明，华南大区负责华南区域销售额。",
    },
]


async def test_retrieve_without_reranker_keeps_rrf_order():
    r = await _retriever_with(_DOCS)
    results = await r.retrieve("销售额渠道", k=2)
    assert results and results[0]["content"].startswith("GMV 销售额")
    assert all(it.get("reranked") is None for it in results)


async def test_rerank_promotes_lower_ranked_doc():
    """重排把 RRF 排在后面的文档顶上来，并标注 reranked。"""
    docs = _DOCS + [
        {
            "id": "d3",
            "title": "口径答疑",
            "content": "销售额口径答疑：GMV 销售额渠道口径的官方解释。",
        }
    ]
    base = await _retriever_with(docs)
    before = await base.retrieve("销售额渠道", k=3)
    lowest = before[-1]["content"]

    # 让重排器把"原始最后一名"打成最高分
    stub = _StubReranker({len(before) - 1: 0.99, 0: 0.1, 1: 0.05})
    reranked = await _retriever_with(docs, reranker=stub)
    result = await reranked.retrieve("销售额渠道", k=3)
    assert len(stub.calls) == 1, "重排应只调用一次（候选批量送出）"
    assert result[0]["content"] == lowest, "重排未生效"
    assert result[0]["reranked"] is True
    assert result[0]["rerank_score"] == 0.99


async def test_rerank_failure_keeps_rrf_order():
    """重排失败 -> 完全保持 RRF 顺序，知识问答不受影响。"""
    docs = _DOCS + [
        {
            "id": "d3",
            "title": "口径答疑",
            "content": "销售额口径答疑：GMV 销售额渠道口径的官方解释。",
        }
    ]
    base = await _retriever_with(docs)
    before = await base.retrieve("销售额渠道", k=3)

    stub = _StubReranker({}, fail=True)
    failing = await _retriever_with(docs, reranker=stub)
    after = await failing.retrieve("销售额渠道", k=3)
    assert [it["content"] for it in after] == [it["content"] for it in before]
    assert all(it.get("reranked") is None for it in after)


async def test_rerank_explicitly_disabled_skips_client():
    docs = _DOCS
    stub = _StubReranker({0: 0.9, 1: 0.1})
    r = await _retriever_with(docs, reranker=stub)
    await r.retrieve("销售额渠道", k=2, rerank=False)
    assert stub.calls == [], "显式关闭重排时不应调用重排服务"


async def test_rerank_config_disabled_skips_client():
    stub = _StubReranker({0: 0.9, 1: 0.1})
    s = Settings(rag=RAGSettings(rerank_enabled=False, rerank_top_n=10))
    r = await _retriever_with(_DOCS, reranker=stub, settings=s)
    await r.retrieve("销售额渠道", k=2)
    assert stub.calls == []


async def test_authority_weighting_prefers_authoritative_doc():
    """同等相关时，权威源（口径字典）必须排在用户上传资料前面。"""
    docs = [
        {
            "id": "user1",
            "title": "我的笔记",
            "content": "销售额口径：GMV 销售额渠道笔记，个人理解版本。",
            "authority_level": "user",
        },
        {
            "id": "auth1",
            "title": "指标口径字典（权威源）",
            "content": "销售额口径：GMV 销售额渠道口径的权威定义。",
            "authority_level": "authoritative",
        },
    ]
    # 关闭重排，只看分级加权对 RRF 分的影响
    s = Settings(rag=RAGSettings(rerank_enabled=False, rerank_top_n=10))
    r = await _retriever_with(docs, settings=s)
    results = await r.retrieve("销售额口径 GMV 渠道", k=2)
    levels = [it["authority_level"] for it in results]
    assert "authoritative" in levels and "user" in levels
    # 权威源与用户源的原始 RRF 相同（内容重叠度高），加权后权威源在前
    by_level = {it["authority_level"]: it for it in results}
    assert by_level["authoritative"]["score"] > by_level["user"]["score"]
    assert results[0]["authority_level"] == "authoritative"


async def test_authority_weight_unknown_level_is_neutral():
    """未知分级按 1.0（不加权也不降权），避免误升权。"""
    docs = [
        {
            "id": "d1",
            "title": "t",
            "content": "销售额口径说明。",
            "authority_level": "unknown-level",
        }
    ]
    r = await _retriever_with(docs)
    results = await r.retrieve("销售额口径", k=1)
    assert results[0]["authority_level"] == "unknown-level"
    assert results[0]["score"] == results[0]["rrf_score"], "未知分级不应改变分数"


# ==================== API 契约（无需数据库） ====================


def test_knowledge_router_exposes_seed_and_stats_routes():
    from dmf_agent.api.app import create_app

    paths = create_app().openapi()["paths"]
    assert "/api/v1/knowledge/seed/dictionary" in paths
    assert "/api/v1/knowledge/authority/stats" in paths
    # 灌入是管理动作：必须挂 admin 守卫（不能只是写权限）
    seed = paths["/api/v1/knowledge/seed/dictionary"]["post"]
    assert seed.get("summary", "").find("管理员") >= 0
