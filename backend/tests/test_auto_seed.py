"""启动自动播种口径字典（Task B）测试。

验证 `_auto_seed_dictionary_if_needed`：
- 目标租户下无 authoritative 文档时才播种（不重复灌）；
- 鉴权关闭：按 tenant_id="" 播种一份；
- 鉴权开启：按各租户分别补齐，且每租户仅一份（幂等）。
"""

from dmf_agent.api.app import _auto_seed_dictionary_if_needed
from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import (
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryTenantRepository,
)
from dmf_agent.rag.vector_store import VectorStore
from dmf_agent.schemas.auth import TenantData
from dmf_agent.semantic import SemanticLayer


class _FakeVectorStore(VectorStore):
    """最小向量库桩：满足 add/delete 接口，不真正向量化。"""

    def __init__(self):
        self.added = 0

    def add(self, texts, metadatas):
        self.added += len(texts)
        return [f"v{i}" for i in range(len(texts))]

    def delete(self, ids):
        return None

    def search(self, query, k):
        return []


async def _db(auth_enabled: bool, tenants=()):
    settings = Settings()
    settings.auth.enabled = auth_enabled
    db = type("_DB", (), {})()
    db.document_repo = InMemoryDocumentRepository()
    db.chunk_repo = InMemoryChunkRepository()
    db.tenant_repo = InMemoryTenantRepository()
    for t in tenants:
        await db.tenant_repo.add(t)
    return settings, db


async def test_auto_seed_when_empty_then_idempotent():
    settings, db = await _db(auth_enabled=False)
    vs = _FakeVectorStore()
    layer = SemanticLayer()

    await _auto_seed_dictionary_if_needed(settings, db, vs, layer)
    docs = await db.document_repo.list(authority_level="authoritative", tenant_id="")
    assert len(docs) == 1, "应为空租户播种一份口径字典"

    # 再次调用：已存在则跳过，不产生第二份
    await _auto_seed_dictionary_if_needed(settings, db, vs, layer)
    docs2 = await db.document_repo.list(authority_level="authoritative", tenant_id="")
    assert len(docs2) == 1, "重复调用不应重复播种"


async def test_auto_seed_per_tenant_when_auth_enabled():
    t1 = TenantData(id="t1", code="t1", name="租户一")
    t2 = TenantData(id="t2", code="t2", name="租户二")
    settings, db = await _db(auth_enabled=True, tenants=[t1, t2])
    vs = _FakeVectorStore()
    layer = SemanticLayer()

    await _auto_seed_dictionary_if_needed(settings, db, vs, layer)
    # 每个租户各一份权威文档
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t1")) == 1
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t2")) == 1

    # 幂等：再次调用不增量
    await _auto_seed_dictionary_if_needed(settings, db, vs, layer)
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t1")) == 1
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t2")) == 1


async def test_auto_seed_skipped_when_no_vector_store():
    settings, db = await _db(auth_enabled=False)
    layer = SemanticLayer()
    # vector_store=None -> 直接跳过，不播种
    await _auto_seed_dictionary_if_needed(settings, db, None, layer)
    assert await db.document_repo.list(authority_level="authoritative") == []
