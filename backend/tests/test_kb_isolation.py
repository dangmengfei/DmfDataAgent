"""知识库检索隔离测试：同租户内个人/私有库互不可见，企业/公开/权威源全员可见。

验证「DMF 知识库重点做」检索期个人/企业隔离修复：
- 个人/私有库下的文档 owner_id = 上传者，仅本人可见；
- 企业库 / 公开库 / owner 为空的系统权威源 对同租户全员可见；
- 跨租户文档不可见（tenant_id 过滤）。
"""

from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import (
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryKnowledgeBaseRepository,
)
from dmf_agent.rag.retriever import HybridKnowledgeRetriever
from dmf_agent.schemas.knowledge import ChunkData, DocumentData, KnowledgeBaseData

MARK = "隔离测试专用词"


def _doc(id_, title, tenant, owner, kb=None):
    return DocumentData(
        id=id_,
        title=title,
        source_type="text",
        knowledge_base_id=kb,
        tenant_id=tenant,
        owner_id=owner,
    )


async def _build():
    kb_repo = InMemoryKnowledgeBaseRepository()
    await kb_repo.add(KnowledgeBaseData(id="kb_e", name="企业库", type="enterprise",
                                       owner_id="", tenant_id="T1", visibility="public"))
    await kb_repo.add(KnowledgeBaseData(id="kb_pa", name="A个人库", type="personal",
                                       owner_id="u_a", tenant_id="T1", visibility="private"))
    await kb_repo.add(KnowledgeBaseData(id="kb_pb", name="B个人库", type="personal",
                                       owner_id="u_b", tenant_id="T1", visibility="private"))

    doc_repo = InMemoryDocumentRepository()
    docs = [
        _doc("d_e", "企业共享", "T1", "", kb="kb_e"),          # 企业/公开：全员可见
        _doc("d_pa", "A个人", "T1", "u_a", kb="kb_pa"),       # A 私有：仅 u_a
        _doc("d_pb", "B个人", "T1", "u_b", kb="kb_pb"),       # B 私有：仅 u_b
        _doc("d_ua", "A未归类", "T1", "u_a"),                 # 未归类按个人：仅 u_a
        _doc("d_auth", "权威源", "T1", ""),                   # owner 空：全员可见
        _doc("d_other", "其他租户", "T2", "u_x"),             # 跨租户：不可见
    ]
    for d in docs:
        await doc_repo.add(d)

    chunk_repo = InMemoryChunkRepository()
    await chunk_repo.add_many(
        [
            ChunkData(id=f"c_{d.id}", document_id=d.id, chunk_index=0,
                      content=f"{MARK} {d.title}")
            for d in docs
        ]
    )
    return kb_repo, doc_repo, chunk_repo


async def _sources(retriever, tenant, owner):
    res = await retriever.retrieve(MARK, k=20, tenant_id=tenant, owner_id=owner)
    return {r["source"] for r in res}


async def test_personal_isolation_within_tenant():
    kb_repo, doc_repo, chunk_repo = await _build()
    retriever = HybridKnowledgeRetriever(None, chunk_repo, doc_repo, Settings(), kb_repo=kb_repo)

    a_sees = await _sources(retriever, "T1", "u_a")
    assert a_sees == {"企业共享", "A个人", "A未归类", "权威源"}, a_sees
    assert "B个人" not in a_sees

    b_sees = await _sources(retriever, "T1", "u_b")
    assert b_sees == {"企业共享", "B个人", "权威源"}, b_sees
    assert "A个人" not in b_sees and "A未归类" not in b_sees


async def test_cross_tenant_invisible():
    kb_repo, doc_repo, chunk_repo = await _build()
    retriever = HybridKnowledgeRetriever(None, chunk_repo, doc_repo, Settings(), kb_repo=kb_repo)
    # T2 租户用户只能看到自己租户的文档，看不到 T1 的任何文档
    seen = await _sources(retriever, "T2", "u_x")
    assert seen == {"其他租户"}, seen
    assert seen.isdisjoint({"企业共享", "A个人", "B个人", "A未归类", "权威源"})


async def test_auth_disabled_no_isolation():
    kb_repo, doc_repo, chunk_repo = await _build()
    retriever = HybridKnowledgeRetriever(None, chunk_repo, doc_repo, Settings(), kb_repo=kb_repo)
    # tenant=None & owner=None -> 不隔离，全量可见
    all_seen = await _sources(retriever, None, None)
    assert all_seen == {"企业共享", "A个人", "B个人", "A未归类", "权威源", "其他租户"}, all_seen
