"""定时增量调度（Task C）测试。

验证：
- 周期重建逻辑：按租户补齐口径字典权威源；
- DictionaryRefreshScheduler 在关闭/缺失依赖时不启用，启用后可启停。
"""

from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import (
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryTenantRepository,
)
from dmf_agent.rag.scheduler import (
    DictionaryRefreshScheduler,
    rebuild_dictionary_for_tenants,
)
from dmf_agent.rag.vector_store import VectorStore
from dmf_agent.schemas.auth import TenantData
from dmf_agent.semantic import SemanticLayer


class _FakeVectorStore(VectorStore):
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


async def test_rebuild_dictionary_seeds_when_empty():
    settings, db = await _db(auth_enabled=False)
    vs = _FakeVectorStore()
    layer = SemanticLayer()

    results = await rebuild_dictionary_for_tenants(settings, db, vs, layer)
    assert results.get("") is not None, "应为无租户补齐一份口径字典"
    docs = await db.document_repo.list(authority_level="authoritative", tenant_id="")
    assert len(docs) == 1


async def test_rebuild_dictionary_per_tenant_when_auth_enabled():
    t1 = TenantData(id="t1", code="t1", name="租户一")
    t2 = TenantData(id="t2", code="t2", name="租户二")
    settings, db = await _db(auth_enabled=True, tenants=[t1, t2])
    vs = _FakeVectorStore()
    layer = SemanticLayer()

    results = await rebuild_dictionary_for_tenants(settings, db, vs, layer)
    assert results.get("t1") is not None and results.get("t2") is not None
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t1")) == 1
    assert len(await db.document_repo.list(authority_level="authoritative", tenant_id="t2")) == 1


async def test_scheduler_start_disabled_returns_false():
    settings = Settings()  # kb_auto_refresh_enabled 默认 False
    scheduler = DictionaryRefreshScheduler()
    ok = scheduler.start(settings, type("_DB", (), {})(), _FakeVectorStore(), SemanticLayer())
    assert ok is False
    assert scheduler.running is False


async def test_scheduler_start_no_vector_store_returns_false():
    settings = Settings()
    settings.rag.kb_auto_refresh_enabled = True
    scheduler = DictionaryRefreshScheduler()
    ok = scheduler.start(settings, type("_DB", (), {})(), None, SemanticLayer())
    assert ok is False
    assert scheduler.running is False


async def test_scheduler_start_enabled_then_stop():
    settings = Settings()
    settings.rag.kb_auto_refresh_enabled = True
    settings.rag.kb_auto_refresh_interval_s = 1
    db = type("_DB", (), {})()
    db.document_repo = InMemoryDocumentRepository()
    db.chunk_repo = InMemoryChunkRepository()
    db.tenant_repo = InMemoryTenantRepository()

    scheduler = DictionaryRefreshScheduler()
    ok = scheduler.start(settings, db, _FakeVectorStore(), SemanticLayer())
    assert ok is True
    assert scheduler.running is True

    # 等待至少一个周期跑完（间隔 1s），确认未崩
    import asyncio

    await asyncio.sleep(1.3)
    assert scheduler.running is True

    await scheduler.stop()
    assert scheduler.running is False
