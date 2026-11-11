"""文档分块查看接口（只读，核验切分/入库质量）。

零外部依赖：conftest 强制 embedding 未配置 -> 向量库 None -> BM25 模式，
入库仍会切分并保存文本分块（vector_ref=None），可完整验证接口链路。
"""

from fastapi.testclient import TestClient

from dmf_agent.api.app import app


def test_document_chunks_endpoint():
    with TestClient(app) as client:
        text = "\n\n".join(
            f"第 {i} 段：这是足够长的内容用于触发多分块。GMV 指标口径说明。{'填充' * 60}"
            for i in range(10)
        )
        r = client.post("/api/v1/knowledge/documents", json={"title": "分块测试", "text": text})
        assert r.status_code == 200
        doc = r.json()
        assert doc["chunk_count"] and doc["chunk_count"] > 1

        r = client.get(f"/api/v1/knowledge/documents/{doc['id']}/chunks")
        assert r.status_code == 200
        chunks = r.json()
        assert len(chunks) == doc["chunk_count"]
        # 按 chunk_index 升序
        assert [c["chunk_index"] for c in chunks] == sorted(c["chunk_index"] for c in chunks)
        assert all(c["document_id"] == doc["id"] for c in chunks)
        assert all(c["content"] for c in chunks)

        # 不存在的文档 -> 404（不泄露存在性）
        assert client.get("/api/v1/knowledge/documents/not_exist/chunks").status_code == 404
