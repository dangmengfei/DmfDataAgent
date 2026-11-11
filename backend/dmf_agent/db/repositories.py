"""MySQL 仓储实现（SQLAlchemy 2.0 async）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dmf_agent.db import models
from dmf_agent.db.redis_client import memory_key
from dmf_agent.semantic.governance import (
    DEPRECATED,
    DRAFT,
    PUBLISHED,
    REVIEW,
    can_transition,
    derive_dimension_key,
    derive_metric_key,
    dimension_core_defn,
    metric_core_defn,
    validate_dimension_definition,
    validate_metric_definition,
)
from dmf_agent.semantic.yaml_defn import build_layer_with_metric, metric_to_yaml_defn
from dmf_agent.schemas.slang import (
    ALL_SCOPES,
    SCOPE_INDUSTRY,
    SCOPE_TENANT,
    SCOPE_USER,
    SOURCE_BUILTIN,
    STATUS_DISABLED,
    STATUS_PUBLISHED,
    SlangEntryData,
    SlangVersionData,
)
from dmf_agent.schemas.knowledge import ChunkData, DocumentData, KnowledgeBaseData
from dmf_agent.schemas.memory import DomainMemoryData, MemorySummaryData, UserPreferenceData
from dmf_agent.schemas.feedback import BadCaseData, FewShotData
from dmf_agent.schemas.metrics import MetricData
from dmf_agent.schemas.session import (
    ConversationStateData,
    MessageData,
    SessionData,
)
from dmf_agent.schemas.auth import RoleData, TenantData, UserData
from dmf_agent.schemas.permission import (
    DataScopeData,
    DimensionPolicyData,
    MetricGrantData,
)
from dmf_agent.schemas.semantic_publish import SemanticPublishedData
from dmf_agent.schemas.trace import (
    SpanData,
    TraceData,
    aggregate_spans,
    aggregate_traces,
)


def _new_id() -> str:
    return uuid4().hex


def _to_session_data(m: models.SessionModel) -> SessionData:
    return SessionData(
        id=m.id, user_id=m.user_id, tenant_id=m.tenant_id or "", title=m.title,
        status=m.status, created_at=m.created_at, updated_at=m.updated_at,
    )


def _to_message_data(m: models.MessageModel) -> MessageData:
    return MessageData(
        id=m.id, session_id=m.session_id, role=m.role,
        content=m.content, node_trace=m.node_trace or {}, created_at=m.created_at,
    )


def _to_document_data(m: models.KnowledgeDocumentModel) -> DocumentData:
    return DocumentData(
        id=m.id, title=m.title, source_type=m.source_type, source_uri=m.source_uri,
        status=m.status, chunk_count=m.chunk_count, knowledge_base_id=m.knowledge_base_id,
        authority_level=m.authority_level or "user",
        tenant_id=m.tenant_id or "",
        owner_id=m.owner_id or "",
        created_at=m.created_at, updated_at=m.updated_at,
    )


def _to_base_data(m: models.KnowledgeBaseModel) -> KnowledgeBaseData:
    return KnowledgeBaseData(
        id=m.id, name=m.name, description=m.description, type=m.type,
        owner_id=m.owner_id, tenant_id=m.tenant_id or "", visibility=m.visibility,
        created_at=m.created_at, updated_at=m.updated_at,
    )


def _to_metric_data(m: models.MetricDefinitionModel) -> MetricData:
    return MetricData(
        code=m.code, name=m.name, description=m.description,
        unit=m.unit, category=m.category, dimensions=m.dimensions or [],
    )


class SqlSessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self, user_id: str, title: Optional[str] = None, tenant_id: str = ""
    ) -> SessionData:
        obj = models.SessionModel(
            id=_new_id(), user_id=user_id, title=title or "新会话", tenant_id=tenant_id
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return _to_session_data(obj)

    async def get(self, session_id: str) -> Optional[SessionData]:
        async with self._sf() as session:
            obj = await session.get(models.SessionModel, session_id)
            return _to_session_data(obj) if obj else None

    async def list_by_user(self, user_id: str) -> List[SessionData]:
        async with self._sf() as session:
            stmt = (
                select(models.SessionModel)
                .where(models.SessionModel.user_id == user_id)
                .order_by(models.SessionModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_session_data(r) for r in rows]

    async def list_by_tenant(self, tenant_id: str) -> List[SessionData]:
        async with self._sf() as session:
            stmt = (
                select(models.SessionModel)
                .where(models.SessionModel.tenant_id == tenant_id)
                .order_by(models.SessionModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_session_data(r) for r in rows]

    async def list_all(self, limit: int = 500) -> List[SessionData]:
        """全部会话（管理面用：记忆管理页补 id→title 映射，避免逐条查）。"""
        async with self._sf() as session:
            stmt = (
                select(models.SessionModel)
                .order_by(models.SessionModel.updated_at.desc())
                .limit(max(limit, 1))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_session_data(r) for r in rows]

    async def rename(self, session_id: str, title: str) -> Optional[SessionData]:
        async with self._sf() as session:
            stmt = (
                update(models.SessionModel)
                .where(models.SessionModel.id == session_id)
                .values(title=title)
            )
            await session.execute(stmt)
            await session.commit()
        return await self.get(session_id)

    async def delete(self, session_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.SessionModel, session_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


class SqlMessageRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, message: MessageData) -> MessageData:
        obj = models.MessageModel(
            id=message.id or _new_id(),
            session_id=message.session_id,
            role=message.role,
            content=message.content,
            node_trace=message.node_trace or {},
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return _to_message_data(obj)

    async def list_by_session(self, session_id: str) -> List[MessageData]:
        async with self._sf() as session:
            stmt = (
                select(models.MessageModel)
                .where(models.MessageModel.session_id == session_id)
                .order_by(models.MessageModel.created_at.asc(), models.MessageModel.id.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_message_data(r) for r in rows]

    async def delete_by_session(self, session_id: str) -> int:
        async with self._sf() as session:
            stmt = select(models.MessageModel).where(
                models.MessageModel.session_id == session_id
            )
            rows = (await session.execute(stmt)).scalars().all()
            count = len(rows)
            for r in rows:
                await session.delete(r)
            await session.commit()
            return count

    async def delete_after(
        self, session_id: str, message_id: str, *, inclusive: bool = True
    ) -> int:
        """删除该会话中 `message_id` 及其之后的全部消息（编辑重发的「替换该轮」）。

        先按 list_by_session 的同口径（created_at, id）取有序 id，定位截断点后再按 id 批量删；
        会话消息量级很小（≤ 数百），避免 MySQL 行值比较的方言坑。
        """
        async with self._sf() as session:
            ids = list(
                (
                    await session.execute(
                        select(models.MessageModel.id)
                        .where(models.MessageModel.session_id == session_id)
                        .order_by(
                            models.MessageModel.created_at.asc(),
                            models.MessageModel.id.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if message_id not in ids:
                return 0  # 锚点不存在（或不属于本会话）→ 不删，避免误伤
            cut = ids.index(message_id) if inclusive else ids.index(message_id) + 1
            victims = ids[cut:]
            if not victims:
                return 0
            rows = (
                (
                    await session.execute(
                        select(models.MessageModel).where(
                            models.MessageModel.id.in_(victims)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                await session.delete(r)
            await session.commit()
            return len(rows)

    async def get(self, message_id: str) -> Optional[MessageData]:
        async with self._sf() as session:
            obj = await session.get(models.MessageModel, message_id)
            return _to_message_data(obj) if obj else None


class SqlDocumentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, doc: DocumentData) -> DocumentData:
        obj = models.KnowledgeDocumentModel(
            id=doc.id or _new_id(),
            title=doc.title,
            source_type=doc.source_type,
            source_uri=doc.source_uri,
            knowledge_base_id=doc.knowledge_base_id,
            authority_level=doc.authority_level or "user",
            tenant_id=doc.tenant_id,
            owner_id=doc.owner_id or "",
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return _to_document_data(obj)

    async def get(self, doc_id: str) -> Optional[DocumentData]:
        async with self._sf() as session:
            obj = await session.get(models.KnowledgeDocumentModel, doc_id)
            return _to_document_data(obj) if obj else None

    async def list(
        self,
        keyword: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        authority_level: Optional[str] = None,
        source_uri: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> List[DocumentData]:
        async with self._sf() as session:
            stmt = select(models.KnowledgeDocumentModel).order_by(
                models.KnowledgeDocumentModel.created_at.desc()
            )
            if keyword:
                stmt = stmt.where(models.KnowledgeDocumentModel.title.like(f"%{keyword}%"))
            if knowledge_base_id:
                stmt = stmt.where(
                    models.KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id
                )
            if authority_level:
                stmt = stmt.where(
                    models.KnowledgeDocumentModel.authority_level == authority_level
                )
            # source_uri 作为权威源灌入的幂等键（seed://<kind>/<version>）
            if source_uri:
                stmt = stmt.where(models.KnowledgeDocumentModel.source_uri == source_uri)
            # 租户过滤：鉴权开启时传当前租户，跨租户文档不可见（None = 不过滤）
            if tenant_id is not None:
                stmt = stmt.where(models.KnowledgeDocumentModel.tenant_id == tenant_id)
            # owner_id 过滤：检索隔离用——只取归属当前用户的个人/私有文档
            if owner_id is not None:
                stmt = stmt.where(models.KnowledgeDocumentModel.owner_id == owner_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_document_data(r) for r in rows]

    async def count_by_authority(self) -> Dict[str, int]:
        """按来源分级统计文档数（供知识库看板与验收）。"""
        async with self._sf() as session:
            stmt = select(
                models.KnowledgeDocumentModel.authority_level, func.count()
            ).group_by(models.KnowledgeDocumentModel.authority_level)
            rows = (await session.execute(stmt)).all()
            return {str(level or "user"): int(cnt) for level, cnt in rows}

    async def count_documents(self, knowledge_base_id: str) -> int:
        async with self._sf() as session:
            stmt = select(models.KnowledgeDocumentModel).where(
                models.KnowledgeDocumentModel.knowledge_base_id == knowledge_base_id
            )
            rows = (await session.execute(stmt)).scalars().all()
            return len(rows)

    async def update_status(
        self, doc_id: str, status: str, chunk_count: Optional[int] = None
    ) -> Optional[DocumentData]:
        values: dict = {"status": status}
        if chunk_count is not None:
            values["chunk_count"] = chunk_count
        async with self._sf() as session:
            stmt = (
                update(models.KnowledgeDocumentModel)
                .where(models.KnowledgeDocumentModel.id == doc_id)
                .values(**values)
            )
            await session.execute(stmt)
            await session.commit()
        return await self.get(doc_id)

    async def delete(self, doc_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.KnowledgeDocumentModel, doc_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


class SqlKnowledgeBaseRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, base: KnowledgeBaseData) -> KnowledgeBaseData:
        obj = models.KnowledgeBaseModel(
            id=base.id or _new_id(),
            name=base.name,
            description=base.description,
            type=base.type,
            owner_id=base.owner_id,
            tenant_id=base.tenant_id,
            visibility=base.visibility,
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            base.id = obj.id
            return _to_base_data(obj)

    async def get(self, base_id: str) -> Optional[KnowledgeBaseData]:
        async with self._sf() as session:
            obj = await session.get(models.KnowledgeBaseModel, base_id)
            return _to_base_data(obj) if obj else None

    async def list(
        self,
        type: Optional[str] = None,
        keyword: Optional[str] = None,
        owner_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[KnowledgeBaseData]:
        async with self._sf() as session:
            stmt = select(models.KnowledgeBaseModel).order_by(
                models.KnowledgeBaseModel.created_at.desc()
            )
            if type:
                stmt = stmt.where(models.KnowledgeBaseModel.type == type)
            if keyword:
                stmt = stmt.where(models.KnowledgeBaseModel.name.like(f"%{keyword}%"))
            if owner_id:
                stmt = stmt.where(models.KnowledgeBaseModel.owner_id == owner_id)
            if tenant_id:
                stmt = stmt.where(models.KnowledgeBaseModel.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_base_data(r) for r in rows]

    async def delete(self, base_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.KnowledgeBaseModel, base_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


class SqlChunkRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add_many(self, chunks: List[ChunkData]) -> List[ChunkData]:
        async with self._sf() as session:
            for c in chunks:
                session.add(
                    models.KnowledgeChunkModel(
                        id=c.id or _new_id(),
                        document_id=c.document_id,
                        chunk_index=c.chunk_index,
                        content=c.content,
                        vector_ref=c.vector_ref,
                    )
                )
            await session.commit()
        return chunks

    async def list_by_document(self, doc_id: str) -> List[ChunkData]:
        async with self._sf() as session:
            stmt = (
                select(models.KnowledgeChunkModel)
                .where(models.KnowledgeChunkModel.document_id == doc_id)
                .order_by(models.KnowledgeChunkModel.chunk_index.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                ChunkData(
                    id=r.id, document_id=r.document_id, chunk_index=r.chunk_index,
                    content=r.content, vector_ref=r.vector_ref,
                )
                for r in rows
            ]

    async def list_all(self) -> List[ChunkData]:
        async with self._sf() as session:
            stmt = select(models.KnowledgeChunkModel)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                ChunkData(
                    id=r.id, document_id=r.document_id, chunk_index=r.chunk_index,
                    content=r.content, vector_ref=r.vector_ref,
                )
                for r in rows
            ]

    async def delete_by_document(self, doc_id: str) -> int:
        async with self._sf() as session:
            stmt = select(models.KnowledgeChunkModel).where(
                models.KnowledgeChunkModel.document_id == doc_id
            )
            rows = (await session.execute(stmt)).scalars().all()
            count = len(rows)
            for r in rows:
                await session.delete(r)
            await session.commit()
            return count


class SqlMemorySummaryRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Any = None,
        ttl: int = 0,
    ) -> None:
        self._sf = session_factory
        # Redis 热层客户端；None 表示未启用，所有读写仅走 MySQL（优雅降级）
        self._redis = redis_client
        self._ttl = ttl

    async def get(self, session_id: str) -> Optional[MemorySummaryData]:
        # 1) 优先读 Redis 热层（命中即返回，并续期 TTL）
        if self._redis is not None:
            cached = await self._redis.get_json(memory_key(session_id))
            if cached:
                if self._ttl and self._ttl > 0:
                    await self._redis.set_json(memory_key(session_id), cached, self._ttl)
                return MemorySummaryData(**cached)
        # 2) 回源 MySQL
        async with self._sf() as session:
            obj = await session.get(models.MemorySummaryModel, session_id)
            if obj is None:
                return None
            data = MemorySummaryData(
                session_id=obj.session_id,
                summary=obj.summary,
                upto_message_id=obj.upto_message_id,
                updated_at=obj.updated_at,
                expires_at=obj.expires_at,
                last_accessed_at=obj.last_accessed_at,
            )
        # 3) 回填 Redis 热层
        if self._redis is not None:
            payload = data.model_dump()
            payload.pop("updated_at", None)
            payload.pop("expires_at", None)
            payload.pop("last_accessed_at", None)
            await self._redis.set_json(memory_key(session_id), payload, self._ttl)
        return data

    async def upsert(self, summary: MemorySummaryData) -> MemorySummaryData:
        async with self._sf() as session:
            obj = await session.get(models.MemorySummaryModel, summary.session_id)
            if obj is None:
                obj = models.MemorySummaryModel(
                    session_id=summary.session_id,
                    summary=summary.summary,
                    upto_message_id=summary.upto_message_id,
                )
                session.add(obj)
            else:
                obj.summary = summary.summary
                obj.upto_message_id = summary.upto_message_id
            await session.commit()
            await session.refresh(obj)
            result = MemorySummaryData(
                session_id=obj.session_id,
                summary=obj.summary,
                upto_message_id=obj.upto_message_id,
                updated_at=obj.updated_at,
                expires_at=obj.expires_at,
                last_accessed_at=obj.last_accessed_at,
            )
        # 双写 Redis 热层（不可用时静默降级）
        if self._redis is not None:
            payload = result.model_dump()
            payload.pop("updated_at", None)
            payload.pop("expires_at", None)
            payload.pop("last_accessed_at", None)
            await self._redis.set_json(memory_key(summary.session_id), payload, self._ttl)
        return result

    async def delete(self, session_id: str) -> None:
        async with self._sf() as session:
            obj = await session.get(models.MemorySummaryModel, session_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()
        if self._redis is not None:
            await self._redis.delete(memory_key(session_id))

    async def list_recent(
        self, limit: int = 100, keyword: str = ""
    ) -> List[MemorySummaryData]:
        """摘要列表（管理面）；只走 MySQL 主存（Redis 只缓存热点单条，不做全量扫描）。"""
        async with self._sf() as session:
            stmt = select(models.MemorySummaryModel)
            kw = (keyword or "").strip()
            if kw:
                stmt = stmt.where(models.MemorySummaryModel.summary.like(f"%{kw}%"))
            stmt = stmt.order_by(models.MemorySummaryModel.updated_at.desc()).limit(
                max(limit, 1)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                MemorySummaryData(
                    session_id=r.session_id,
                    summary=r.summary,
                    upto_message_id=r.upto_message_id or "",
                    updated_at=r.updated_at,
                    expires_at=r.expires_at,
                    last_accessed_at=r.last_accessed_at,
                )
                for r in rows
            ]

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (await session.execute(select(func.count()).select_from(
                    models.MemorySummaryModel
                ))).scalar() or 0
            )


class SqlConversationStateRepository:
    """结构化对话状态 MySQL 仓储（P0-6 多轮对话状态管理）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_data(obj: models.ConversationStateModel) -> ConversationStateData:
        return ConversationStateData(
            session_id=obj.session_id,
            tenant_id=obj.tenant_id,
            user_id=obj.user_id,
            intent=obj.intent,
            active_metrics=list(obj.active_metrics or []),
            active_dimensions=list(obj.active_dimensions or []),
            active_filters=dict(obj.active_filters or {}),
            time_range=dict(obj.time_range or {}),
            topic=obj.topic or "",
            last_query=obj.last_query or "",
            last_query_rewritten=obj.last_query_rewritten or "",
            pending_clarification=dict(obj.pending_clarification or {}),
            turn_count=obj.turn_count or 0,
            clarification_count=obj.clarification_count or 0,
            status=obj.status or "active",
            updated_at=obj.updated_at,
        )

    async def get(self, session_id: str) -> Optional[ConversationStateData]:
        async with self._sf() as session:
            obj = await session.get(models.ConversationStateModel, session_id)
            if obj is None:
                return None
            return self._to_data(obj)

    async def upsert(self, state: ConversationStateData) -> ConversationStateData:
        async with self._sf() as session:
            obj = await session.get(models.ConversationStateModel, state.session_id)
            if obj is None:
                obj = models.ConversationStateModel(session_id=state.session_id)
                session.add(obj)
            obj.tenant_id = state.tenant_id
            obj.user_id = state.user_id
            obj.intent = state.intent
            obj.active_metrics = list(state.active_metrics or [])
            obj.active_dimensions = list(state.active_dimensions or [])
            obj.active_filters = dict(state.active_filters or {})
            obj.time_range = dict(state.time_range or {})
            obj.topic = state.topic or ""
            obj.last_query = state.last_query or ""
            obj.last_query_rewritten = state.last_query_rewritten or ""
            obj.pending_clarification = dict(state.pending_clarification or {})
            obj.turn_count = state.turn_count or 0
            obj.clarification_count = state.clarification_count or 0
            obj.status = state.status or "active"
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def delete(self, session_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.ConversationStateModel, session_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def list_recent(
        self, limit: int = 100, tenant_id: Optional[str] = None
    ) -> List[ConversationStateData]:
        stmt = select(models.ConversationStateModel)
        if tenant_id is not None:
            stmt = stmt.where(models.ConversationStateModel.tenant_id == tenant_id)
        stmt = stmt.order_by(models.ConversationStateModel.updated_at.desc()).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]


class SqlMetricRepository:
    """指标元数据 MySQL 仓储。

    TODO(v2-migration): 当前为精确/包含匹配，指标量大后需迁移老系统的
    召回策略（plugin/utils/AC_recall.py AC 自动机 +
    bm25_recall.py BM25 + embedding 召回）。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def find_by_name_or_code(self, name_or_code: str) -> Optional[MetricData]:
        text = name_or_code.strip()
        async with self._sf() as session:
            stmt = select(models.MetricDefinitionModel).where(
                or_(
                    models.MetricDefinitionModel.code == text,
                    models.MetricDefinitionModel.name == text,
                    models.MetricDefinitionModel.name.like(f"%{text}%"),
                )
            )
            row = (await session.execute(stmt)).scalars().first()
            return _to_metric_data(row) if row else None

    async def search(self, keyword: Optional[str] = None, limit: int = 20) -> List[MetricData]:
        async with self._sf() as session:
            stmt = select(models.MetricDefinitionModel)
            if keyword:
                stmt = stmt.where(
                    or_(
                        models.MetricDefinitionModel.name.like(f"%{keyword}%"),
                        models.MetricDefinitionModel.code.like(f"%{keyword}%"),
                        models.MetricDefinitionModel.description.like(f"%{keyword}%"),
                    )
                )
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_metric_data(r) for r in rows]

    async def list_all(self) -> List[MetricData]:
        return await self.search(limit=10000)

    async def upsert_many(self, metrics: List[MetricData]) -> int:
        """批量 upsert（按 code 主键），供指标元数据导入使用。"""
        if not metrics:
            return 0
        rows = [
            {
                "code": m.code,
                "name": m.name,
                "description": m.description,
                "unit": m.unit,
                "category": m.category,
                "dimensions": m.dimensions,
            }
            for m in metrics
        ]
        stmt = mysql_insert(models.MetricDefinitionModel).values(rows)
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            description=stmt.inserted.description,
            unit=stmt.inserted.unit,
            category=stmt.inserted.category,
            dimensions=stmt.inserted.dimensions,
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()
        return len(rows)


class SqlSlangRepository:
    """黑话词典 MySQL 实现（三级：行业级 / 公司级 / 用户级）。

    权威写入源：所有写操作先落 MySQL，提交后再由调用方（SlangResolver）同步 Redis。
    每次变更都写一份 `slang_entry_versions` 审计快照。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ---------- 内部工具 ----------
    @staticmethod
    def _to_data(r: models.SlangEntryModel) -> SlangEntryData:
        return SlangEntryData(
            id=r.id,
            scope=r.scope,
            tenant_id=r.tenant_id or "",
            user_id=r.user_id or "",
            term=r.term,
            standard=r.standard or "",
            entry_type=r.entry_type,
            dim_value=r.dim_value or "",
            rewrite_mode=r.rewrite_mode,
            priority=r.priority,
            source=r.source,
            status=r.status,
            version=r.version,
            confidence=float(r.confidence or 0),
            hit_count=r.hit_count or 0,
            remark=r.remark or "",
            created_by=r.created_by or "",
            updated_by=r.updated_by or "",
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    @classmethod
    def _to_version(cls, r: models.SlangEntryVersionModel) -> SlangVersionData:
        return SlangVersionData(
            id=r.id,
            entry_id=r.entry_id,
            version=r.version,
            change_type=r.change_type,
            before_json=r.before_json,
            after_json=r.after_json,
            operator=r.operator or "",
            created_at=r.created_at,
        )

    async def _audit(
        self,
        session: AsyncSession,
        *,
        entry_id: int,
        version: int,
        change_type: str,
        before: Optional[SlangEntryData],
        after: Optional[SlangEntryData],
        operator: str = "",
    ) -> None:
        session.add(
            models.SlangEntryVersionModel(
                entry_id=entry_id,
                version=version,
                change_type=change_type,
                before_json=before.model_dump(mode="json") if before else None,
                after_json=after.model_dump(mode="json") if after else None,
                operator=operator or "",
            )
        )

    # ---------- 解析链路（只读，一次取齐三层） ----------
    async def list_layers(
        self, tenant_id: str = "", user_id: str = ""
    ) -> Dict[str, List[SlangEntryData]]:
        clauses = [models.SlangEntryModel.scope == SCOPE_INDUSTRY]
        if tenant_id:
            clauses.append(models.SlangEntryModel.scope == SCOPE_TENANT)
        if tenant_id and user_id:
            clauses.append(models.SlangEntryModel.scope == SCOPE_USER)
        stmt = select(models.SlangEntryModel).where(
            models.SlangEntryModel.status == STATUS_PUBLISHED, or_(*clauses)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        result: Dict[str, List[SlangEntryData]] = {
            SCOPE_INDUSTRY: [],
            SCOPE_TENANT: [],
            SCOPE_USER: [],
        }
        for r in rows:
            data = self._to_data(r)
            # 归属校验：公司级/用户级必须严格匹配当前身份，避免读到他人词条
            if data.scope == SCOPE_TENANT and data.tenant_id != tenant_id:
                continue
            if data.scope == SCOPE_USER and (
                data.tenant_id != tenant_id or data.user_id != user_id
            ):
                continue
            result.setdefault(data.scope, []).append(data)
        return result

    # ---------- 管理面 ----------
    async def query(
        self,
        *,
        scope: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        entry_type: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[SlangEntryData], int]:
        clauses = []
        if scope:
            clauses.append(models.SlangEntryModel.scope == scope)
        if tenant_id is not None:
            clauses.append(models.SlangEntryModel.tenant_id == tenant_id)
        if user_id is not None:
            clauses.append(models.SlangEntryModel.user_id == user_id)
        if entry_type:
            clauses.append(models.SlangEntryModel.entry_type == entry_type)
        if status:
            clauses.append(models.SlangEntryModel.status == status)
        if source:
            clauses.append(models.SlangEntryModel.source == source)
        if keyword:
            like = f"%{keyword}%"
            clauses.append(
                or_(
                    models.SlangEntryModel.term.like(like),
                    models.SlangEntryModel.standard.like(like),
                    models.SlangEntryModel.remark.like(like),
                )
            )
        base = select(models.SlangEntryModel)
        counter = select(func.count()).select_from(models.SlangEntryModel)
        for c in clauses:
            base = base.where(c)
            counter = counter.where(c)
        base = (
            base.order_by(
                models.SlangEntryModel.scope.asc(),
                models.SlangEntryModel.priority.desc(),
                models.SlangEntryModel.id.desc(),
            )
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        async with self._sf() as session:
            total = int((await session.execute(counter)).scalar() or 0)
            rows = (await session.execute(base)).scalars().all()
        return [self._to_data(r) for r in rows], total

    async def get(self, entry_id: int) -> Optional[SlangEntryData]:
        async with self._sf() as session:
            r = await session.get(models.SlangEntryModel, entry_id)
            return self._to_data(r) if r else None

    async def find(self, entry: SlangEntryData) -> Optional[SlangEntryData]:
        stmt = select(models.SlangEntryModel).where(
            models.SlangEntryModel.scope == entry.scope,
            models.SlangEntryModel.tenant_id == entry.tenant_id,
            models.SlangEntryModel.user_id == entry.user_id,
            models.SlangEntryModel.entry_type == entry.entry_type,
            models.SlangEntryModel.term == entry.term,
        )
        async with self._sf() as session:
            r = (await session.execute(stmt)).scalars().first()
            return self._to_data(r) if r else None

    async def add(self, entry: SlangEntryData) -> SlangEntryData:
        model = models.SlangEntryModel(
            scope=entry.scope,
            tenant_id=entry.tenant_id,
            user_id=entry.user_id,
            term=entry.term,
            standard=entry.standard,
            entry_type=entry.entry_type,
            dim_value=entry.dim_value,
            rewrite_mode=entry.rewrite_mode,
            priority=entry.priority,
            source=entry.source,
            status=entry.status,
            version=1,
            confidence=entry.confidence,
            hit_count=0,
            remark=entry.remark,
            created_by=entry.created_by,
            updated_by=entry.updated_by or entry.created_by,
        )
        async with self._sf() as session:
            session.add(model)
            # 必须先 commit + refresh 再取快照：`created_at/updated_at` 是 server_default，
            # flush 后这两个列仍是"未加载"，直接读会触发隐式 IO ->
            # async 下抛 MissingGreenlet（本项目其它仓储统一用 commit -> refresh 规避）。
            await session.commit()
            await session.refresh(model)
            saved = self._to_data(model)
            await self._audit(
                session,
                entry_id=model.id,
                version=1,
                change_type="create",
                before=None,
                after=saved,
                operator=entry.created_by,
            )
            await session.commit()
            return saved

    async def update(
        self,
        entry_id: int,
        *,
        term: Optional[str] = None,
        standard: Optional[str] = None,
        entry_type: Optional[str] = None,
        dim_value: Optional[str] = None,
        rewrite_mode: Optional[str] = None,
        priority: Optional[int] = None,
        status: Optional[str] = None,
        remark: Optional[str] = None,
        operator: str = "",
    ) -> Optional[SlangEntryData]:
        async with self._sf() as session:
            r = await session.get(models.SlangEntryModel, entry_id)
            if r is None:
                return None
            before = self._to_data(r)
            if term is not None:
                r.term = term
            if standard is not None:
                r.standard = standard
            if entry_type is not None:
                r.entry_type = entry_type
            if dim_value is not None:
                r.dim_value = dim_value
            if rewrite_mode is not None:
                r.rewrite_mode = rewrite_mode
            if priority is not None:
                r.priority = priority
            if remark is not None:
                r.remark = remark
            change_type = "update"
            if status is not None and status != r.status:
                r.status = status
                change_type = "publish" if status == STATUS_PUBLISHED else (
                    "disable" if status == STATUS_DISABLED else "update"
                )
            r.version = (r.version or 1) + 1
            r.updated_by = operator or r.updated_by
            # 同 add()：commit + refresh 后再取快照，避免读 server_default/onupdate 列
            # （updated_at）触发隐式 IO。
            await session.commit()
            await session.refresh(r)
            after = self._to_data(r)
            await self._audit(
                session,
                entry_id=entry_id,
                version=after.version,
                change_type=change_type,
                before=before,
                after=after,
                operator=operator,
            )
            await session.commit()
            return after

    async def delete(self, entry_id: int, operator: str = "") -> bool:
        async with self._sf() as session:
            r = await session.get(models.SlangEntryModel, entry_id)
            if r is None:
                return False
            before = self._to_data(r)
            await session.delete(r)
            await self._audit(
                session,
                entry_id=entry_id,
                version=before.version,
                change_type="delete",
                before=before,
                after=None,
                operator=operator,
            )
            await session.commit()
            return True

    async def count_by_scope(self) -> Dict[str, int]:
        stmt = select(
            models.SlangEntryModel.scope, func.count()
        ).group_by(models.SlangEntryModel.scope)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        counts = {s: 0 for s in ALL_SCOPES}
        for scope, n in rows:
            counts[str(scope)] = int(n)
        return counts

    async def count(self, status: Optional[str] = None) -> int:
        stmt = select(func.count()).select_from(models.SlangEntryModel)
        if status:
            stmt = stmt.where(models.SlangEntryModel.status == status)
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar() or 0)

    async def add_version(self, version: SlangVersionData) -> SlangVersionData:
        model = models.SlangEntryVersionModel(
            entry_id=version.entry_id,
            version=version.version,
            change_type=version.change_type,
            before_json=version.before_json,
            after_json=version.after_json,
            operator=version.operator,
        )
        async with self._sf() as session:
            session.add(model)
            await session.commit()
            return self._to_version(model)

    async def list_versions(self, entry_id: int) -> List[SlangVersionData]:
        stmt = (
            select(models.SlangEntryVersionModel)
            .where(models.SlangEntryVersionModel.entry_id == entry_id)
            .order_by(models.SlangEntryVersionModel.version.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [self._to_version(r) for r in rows]

    async def bump_hit(self, entry_ids: List[int]) -> int:
        ids = [i for i in entry_ids if i]
        if not ids:
            return 0
        stmt = (
            update(models.SlangEntryModel)
            .where(models.SlangEntryModel.id.in_(ids))
            .values(hit_count=models.SlangEntryModel.hit_count + 1)
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()
        return len(ids)

    async def replace_scope_entries(
        self, scope: str, tenant_id: str, user_id: str, entries: List[SlangEntryData]
    ) -> int:
        """整体替换某层的**内置种子**（不动手工/导入词条）。"""
        async with self._sf() as session:
            await session.execute(
                delete(models.SlangEntryModel).where(
                    models.SlangEntryModel.scope == scope,
                    models.SlangEntryModel.tenant_id == tenant_id,
                    models.SlangEntryModel.user_id == user_id,
                    models.SlangEntryModel.source == SOURCE_BUILTIN,
                )
            )
            for e in entries:
                session.add(
                    models.SlangEntryModel(
                        scope=scope,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        term=e.term,
                        standard=e.standard,
                        entry_type=e.entry_type,
                        dim_value=e.dim_value,
                        rewrite_mode=e.rewrite_mode,
                        priority=e.priority,
                        source=SOURCE_BUILTIN,
                        status=STATUS_PUBLISHED,
                        version=1,
                        confidence=e.confidence,
                        remark=e.remark,
                        created_by="builtin",
                        updated_by="builtin",
                    )
                )
            await session.commit()
        return len(entries)

    async def log_rewrite_event(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query_hash: str,
        query_text: str,
        hit_count: int,
        is_rewritten: bool,
        session_id: str = "",
        message_id: str = "",
    ) -> int:
        model = models.SlangRewriteEventModel(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            message_id=message_id,
            query_hash=query_hash,
            query_text=query_text[:512],
            hit_count=hit_count,
            is_rewritten=1 if is_rewritten else 0,
        )
        async with self._sf() as session:
            session.add(model)
            await session.commit()
            await session.refresh(model)
        return model.id

    async def set_rewrite_feedback(
        self,
        *,
        query_hash: str,
        is_correct: bool,
        note: str = "",
        session_id: str = "",
        message_id: str = "",
        cancel: bool = False,
    ) -> int:
        async with self._sf() as session:
            # 定位：优先按 message_id（与状态回显同口径），回退到 query_hash
            row = None
            if message_id:
                stmt = (
                    select(models.SlangRewriteEventModel)
                    .where(models.SlangRewriteEventModel.message_id == message_id)
                    .order_by(models.SlangRewriteEventModel.id.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalars().first()
            if row is None and query_hash:
                stmt = (
                    select(models.SlangRewriteEventModel)
                    .where(models.SlangRewriteEventModel.query_hash == query_hash)
                    .order_by(models.SlangRewriteEventModel.id.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalars().first()
            if cancel:
                # 撤销反馈：把事件 feedback 置 NULL（幂等：无对应事件视为已取消）
                if row is not None:
                    row.feedback = None
                    if note:
                        row.note = note[:255] or row.note
                    await session.commit()
                    await session.refresh(row)
                    return row.id
                return 0
            value = 1 if is_correct else -1
            if row is not None:
                row.feedback = value
                row.note = note[:255]
                if session_id:
                    row.session_id = session_id
                if message_id:
                    row.message_id = message_id
                await session.commit()
                await session.refresh(row)
                return row.id
            # 无对应事件（如改写节点未落库）：新建一条仅含反馈的记录
            model = models.SlangRewriteEventModel(
                tenant_id="",
                user_id="",
                session_id=session_id,
                message_id=message_id,
                query_hash=query_hash,
                query_text="",
                hit_count=0,
                is_rewritten=0,
                feedback=value,
                note=note[:255],
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return model.id

    async def get_rewrite_feedback_by_messages(
        self, message_ids: List[str]
    ) -> Dict[str, int]:
        """按 message_id 批量取改写反馈值（1=正确/-1=误改写），供历史消息回显。"""
        if not message_ids:
            return {}
        async with self._sf() as session:
            stmt = select(models.SlangRewriteEventModel).where(
                models.SlangRewriteEventModel.message_id.in_(message_ids)
            )
            rows = (await session.execute(stmt)).scalars().all()
        result: Dict[str, int] = {}
        for r in rows:
            if r.message_id and r.feedback is not None:
                result[r.message_id] = r.feedback
        return result

    async def query_rewrite_events(
        self, since: Optional[datetime] = None, limit: int = 10000
    ) -> List[Dict[str, Any]]:
        stmt = select(models.SlangRewriteEventModel)
        if since is not None:
            stmt = stmt.where(models.SlangRewriteEventModel.created_at >= since)
        stmt = stmt.order_by(models.SlangRewriteEventModel.id.desc()).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "tenant_id": r.tenant_id,
                "user_id": r.user_id,
                "session_id": r.session_id,
                "message_id": r.message_id,
                "query_hash": r.query_hash,
                "query_text": r.query_text,
                "hit_count": r.hit_count,
                "is_rewritten": bool(r.is_rewritten),
                "feedback": r.feedback,
                "note": r.note,
            }
            for r in rows
        ]

    async def count_rewrite_events(self, since: Optional[datetime] = None) -> int:
        stmt = select(func.count()).select_from(models.SlangRewriteEventModel)
        if since is not None:
            stmt = stmt.where(models.SlangRewriteEventModel.created_at >= since)
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar() or 0


class SqlTraceRepository:
    """对话观测 trace 的 MySQL 实现。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    def _to_data(self, r: models.TraceRecordModel) -> TraceData:
        return TraceData(
            id=r.id,
            session_id=r.session_id,
            round_id=r.round_id,
            intent=r.intent,
            mock_mode=r.mock_mode,
            skills=r.skills or [],
            elapsed_ms=r.elapsed_ms,
            has_error=r.has_error,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            total_tokens=r.total_tokens,
            # FR11 成本与归属（迁移 0021）
            user_id=r.user_id or "",
            tenant_id=r.tenant_id or "",
            model=r.model or "",
            cost=float(r.cost or 0.0),
            trace=r.trace or {},
            created_at=r.created_at,
        )

    async def add(self, record: TraceData) -> TraceData:
        async with self._sf() as session:
            obj = models.TraceRecordModel(
                session_id=record.session_id,
                round_id=record.round_id,
                intent=record.intent,
                mock_mode=record.mock_mode,
                skills=record.skills,
                elapsed_ms=record.elapsed_ms,
                has_error=record.has_error,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                user_id=record.user_id or "",
                tenant_id=record.tenant_id or "",
                model=record.model or "",
                cost=float(record.cost or 0.0),
                trace=record.trace,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def list_recent(self, limit: int = 50) -> List[TraceData]:
        async with self._sf() as session:
            stmt = (
                select(models.TraceRecordModel)
                .order_by(models.TraceRecordModel.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    async def get(self, session_id: str, round_id: str) -> Optional[TraceData]:
        async with self._sf() as session:
            stmt = select(models.TraceRecordModel).where(
                models.TraceRecordModel.session_id == session_id,
                models.TraceRecordModel.round_id == round_id,
            )
            row = (await session.execute(stmt)).scalars().first()
            return self._to_data(row) if row else None

    async def aggregate(self, limit: int = 500) -> dict:
        records = await self.list_recent(limit)
        return aggregate_traces(records)

    # ---- 过滤 / 分页（观测专页） ----
    def _conditions(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
    ) -> list:
        conds = []
        if session_id:
            conds.append(models.TraceRecordModel.session_id == session_id)
        if intent:
            conds.append(models.TraceRecordModel.intent == intent)
        if has_error is not None:
            conds.append(models.TraceRecordModel.has_error == bool(has_error))
        if keyword:
            # intent / skills 为结构化列；正文与改写问句在 trace JSON 内，做宽松 LIKE 匹配
            like = f"%{keyword}%"
            conds.append(
                or_(
                    models.TraceRecordModel.intent.like(like),
                    models.TraceRecordModel.trace.like(like),
                )
            )
        return conds

    async def query(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[TraceData], int]:
        conds = self._conditions(
            session_id=session_id, intent=intent, has_error=has_error, keyword=keyword
        )
        async with self._sf() as session:
            count_stmt = select(func.count()).select_from(models.TraceRecordModel)
            if conds:
                count_stmt = count_stmt.where(*conds)
            total = int((await session.execute(count_stmt)).scalar() or 0)

            stmt = select(models.TraceRecordModel)
            if conds:
                stmt = stmt.where(*conds)
            stmt = (
                stmt.order_by(models.TraceRecordModel.created_at.desc())
                .offset(max(offset, 0))
                .limit(max(limit, 1))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows], total

    async def aggregate_filtered(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        records, _ = await self.query(
            session_id=session_id,
            intent=intent,
            has_error=has_error,
            keyword=keyword,
            limit=limit,
            offset=0,
        )
        return aggregate_traces(records)

    # ---- FR11 成本归集 ----
    @staticmethod
    def _group_column(group_by: str):
        """分组键 -> SQL 表达式（未知分组回落到 model，避免拼接任意列名）。"""
        table = models.TraceRecordModel
        mapping = {
            "model": table.model,
            "user": table.user_id,
            "tenant": table.tenant_id,
            "intent": table.intent,
        }
        if group_by in mapping:
            return mapping[group_by]
        if group_by == "day":
            return func.date(table.created_at)
        return table.model

    async def cost_breakdown(
        self,
        *,
        group_by: str = "model",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        table = models.TraceRecordModel
        group_col = self._group_column(group_by)
        stmt = select(
            group_col.label("key"),
            func.count(table.id).label("rounds"),
            func.coalesce(func.sum(table.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(table.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(table.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(table.cost), 0).label("cost"),
        )
        if since is not None:
            stmt = stmt.where(table.created_at >= since)
        if until is not None:
            stmt = stmt.where(table.created_at < until)
        stmt = stmt.group_by(group_col).order_by(func.coalesce(func.sum(table.cost), 0).desc()).limit(
            max(limit, 1)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [
            {
                "key": r.key if isinstance(r.key, str) else str(r.key or ""),
                "rounds": int(r.rounds or 0),
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "total_tokens": int(r.total_tokens or 0),
                "cost": round(float(r.cost or 0.0), 6),
            }
            for r in rows
        ]


class SqlSpanRepository:
    """OpenTelemetry span 的 MySQL 实现。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_data(r: models.TraceSpanModel) -> SpanData:
        return SpanData(
            id=r.id,
            trace_id=r.trace_id,
            span_id=r.span_id,
            parent_span_id=r.parent_span_id,
            session_id=r.session_id,
            round_id=r.round_id,
            name=r.name,
            kind=r.kind,
            start_time=r.start_time,
            end_time=r.end_time,
            duration_ms=r.duration_ms,
            status_code=r.status_code,
            status_message=r.status_message,
            attributes=r.span_attributes or {},
            events=r.events or [],
            service_name=r.service_name,
            created_at=r.created_at,
        )

    async def add_many(self, spans: List[SpanData]) -> int:
        if not spans:
            return 0
        async with self._sf() as session:
            session.add_all(
                [
                    models.TraceSpanModel(
                        trace_id=s.trace_id,
                        span_id=s.span_id,
                        parent_span_id=s.parent_span_id,
                        session_id=s.session_id,
                        round_id=s.round_id,
                        name=s.name,
                        kind=s.kind,
                        start_time=s.start_time,
                        end_time=s.end_time,
                        duration_ms=s.duration_ms,
                        status_code=s.status_code,
                        status_message=s.status_message,
                        span_attributes=s.attributes,
                        events=s.events,
                        service_name=s.service_name,
                    )
                    for s in spans
                ]
            )
            await session.commit()
        return len(spans)

    async def list_by_round(
        self, session_id: str, round_id: str
    ) -> List[SpanData]:
        async with self._sf() as session:
            stmt = (
                select(models.TraceSpanModel)
                .where(
                    models.TraceSpanModel.session_id == session_id,
                    models.TraceSpanModel.round_id == round_id,
                )
                .order_by(models.TraceSpanModel.start_time, models.TraceSpanModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    async def list_by_session(
        self, session_id: str, limit: int = 500
    ) -> List[SpanData]:
        async with self._sf() as session:
            stmt = (
                select(models.TraceSpanModel)
                .where(models.TraceSpanModel.session_id == session_id)
                .order_by(
                    models.TraceSpanModel.start_time.desc(),
                    models.TraceSpanModel.id.desc(),
                )
                .limit(max(limit, 1))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows][::-1]

    async def list_recent(self, limit: int = 200) -> List[SpanData]:
        async with self._sf() as session:
            stmt = (
                select(models.TraceSpanModel)
                .order_by(
                    models.TraceSpanModel.id.desc(),
                )
                .limit(max(limit, 1))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    async def aggregate(self, limit: int = 2000) -> Dict[str, Any]:
        spans = await self.list_recent(limit)
        return aggregate_spans(spans)


class SqlFewShotRepository:
    """few-shot 样本库（点赞沉淀 + 管理面手工录入）的 MySQL 实现。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    def _to_data(self, r: models.FewShotExampleModel) -> FewShotData:
        return FewShotData(
            id=r.id,
            session_id=r.session_id or "",
            message_id=r.message_id or "",
            intent=r.intent,
            category=r.category or "intention",
            question=r.question,
            answer=r.answer,
            score=r.score,
            source=r.source or "like",
            status=r.status or "active",
            weight=float(r.weight if r.weight is not None else 1.0),
            tags=r.tags or "",
            created_at=r.created_at,
        )

    async def add(self, example: FewShotData) -> FewShotData:
        async with self._sf() as session:
            # 同一消息重复点赞：累加 score（幂等避免重复入库）；手工样本 message_id 为空，不走此分支
            if example.message_id:
                existing = (
                    await session.execute(
                        select(models.FewShotExampleModel).where(
                            models.FewShotExampleModel.message_id == example.message_id
                        )
                    )
                ).scalars().first()
                if existing is not None:
                    existing.score = (existing.score or 0) + 1
                    await session.commit()
                    await session.refresh(existing)
                    return self._to_data(existing)
            obj = models.FewShotExampleModel(
                session_id=example.session_id or "",
                message_id=example.message_id or "",
                intent=example.intent,
                category=example.category or "intention",
                question=example.question,
                answer=example.answer,
                score=example.score or 1,
                source=example.source or "like",
                status=example.status or "active",
                weight=example.weight if example.weight is not None else 1.0,
                tags=example.tags or "",
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def list_all(self, status: Optional[str] = None) -> List[FewShotData]:
        async with self._sf() as session:
            stmt = select(models.FewShotExampleModel)
            if status:
                stmt = stmt.where(models.FewShotExampleModel.status == status)
            stmt = stmt.order_by(models.FewShotExampleModel.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    def _conditions(
        self,
        *,
        category: Optional[str] = None,
        intent: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Any]:
        conds: List[Any] = []
        if category:
            conds.append(models.FewShotExampleModel.category == category)
        if intent:
            conds.append(models.FewShotExampleModel.intent == intent)
        if source:
            conds.append(models.FewShotExampleModel.source == source)
        if status:
            conds.append(models.FewShotExampleModel.status == status)
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            # 问题 / 答案 / 标签三处匹配：管理页搜索框的直觉就是"哪都找一下"
            conds.append(
                or_(
                    models.FewShotExampleModel.question.like(like),
                    models.FewShotExampleModel.answer.like(like),
                    models.FewShotExampleModel.tags.like(like),
                )
            )
        return conds

    async def query(
        self,
        *,
        category: Optional[str] = None,
        intent: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[FewShotData], int]:
        conds = self._conditions(
            category=category, intent=intent, source=source,
            status=status, keyword=keyword,
        )
        async with self._sf() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(models.FewShotExampleModel)
                        .where(*conds)
                    )
                ).scalar()
                or 0
            )
            stmt = (
                select(models.FewShotExampleModel)
                .where(*conds)
                .order_by(models.FewShotExampleModel.created_at.desc())
                .limit(max(limit, 1))
                .offset(max(offset, 0))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows], total

    async def get_by_message(self, message_id: str) -> Optional[FewShotData]:
        if not message_id:
            return None
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(models.FewShotExampleModel).where(
                        models.FewShotExampleModel.message_id == message_id
                    )
                )
            ).scalars().first()
            return self._to_data(row) if row else None

    async def delete_by_message(self, message_id: str) -> bool:
        """按消息撤销点赞样本（用户再点一次点赞=取消）；返回是否有删除。"""
        if not message_id:
            return False
        async with self._sf() as session:
            result = await session.execute(
                delete(models.FewShotExampleModel).where(
                    models.FewShotExampleModel.message_id == message_id
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def get_by_id(self, item_id: int) -> Optional[FewShotData]:
        async with self._sf() as session:
            obj = await session.get(models.FewShotExampleModel, item_id)
            return self._to_data(obj) if obj else None

    async def find_by_question(
        self, question: str, category: Optional[str] = None
    ) -> Optional[FewShotData]:
        async with self._sf() as session:
            stmt = select(models.FewShotExampleModel).where(
                models.FewShotExampleModel.question == question
            )
            if category:
                stmt = stmt.where(models.FewShotExampleModel.category == category)
            row = (await session.execute(stmt)).scalars().first()
            return self._to_data(row) if row else None

    async def update(
        self,
        item_id: int,
        *,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        intent: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[str] = None,
        weight: Optional[float] = None,
        status: Optional[str] = None,
    ) -> Optional[FewShotData]:
        fields: Dict[str, Any] = {}
        for name, value in (
            ("question", question), ("answer", answer), ("intent", intent),
            ("category", category), ("tags", tags), ("weight", weight),
            ("status", status),
        ):
            if value is not None:
                fields[name] = value
        if not fields:
            return await self.get_by_id(item_id)
        async with self._sf() as session:
            obj = await session.get(models.FewShotExampleModel, item_id)
            if obj is None:
                return None
            for name, value in fields.items():
                setattr(obj, name, value)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def delete(self, item_id: int) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.FewShotExampleModel, item_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def count(self, status: Optional[str] = None, source: Optional[str] = None) -> int:
        conds = self._conditions(status=status, source=source)
        async with self._sf() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(models.FewShotExampleModel)
                        .where(*conds)
                    )
                ).scalar()
                or 0
            )


class SqlBadCaseRepository:
    """点踩样本库（bad case）的 MySQL 实现。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    def _to_data(self, r: models.BadCaseModel) -> BadCaseData:
        return BadCaseData(
            id=r.id,
            session_id=r.session_id,
            message_id=r.message_id,
            intent=r.intent,
            question=r.question,
            answer=r.answer,
            reason=r.reason,
            created_at=r.created_at,
        )

    async def add(self, case: BadCaseData) -> BadCaseData:
        async with self._sf() as session:
            obj = models.BadCaseModel(
                session_id=case.session_id,
                message_id=case.message_id,
                intent=case.intent,
                question=case.question,
                answer=case.answer,
                reason=case.reason,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def list_recent(self, limit: int = 50) -> List[BadCaseData]:
        async with self._sf() as session:
            stmt = (
                select(models.BadCaseModel)
                .order_by(models.BadCaseModel.created_at.desc())
                .limit(max(limit, 1))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    async def get_by_message(self, message_id: str) -> Optional[BadCaseData]:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(models.BadCaseModel).where(
                        models.BadCaseModel.message_id == message_id
                    )
                )
            ).scalars().first()
            return self._to_data(row) if row else None

    async def delete_by_message(self, message_id: str) -> bool:
        """按消息撤销 bad-case（用户再点一次点踩=取消）。"""
        if not message_id:
            return False
        async with self._sf() as session:
            result = await session.execute(
                delete(models.BadCaseModel).where(
                    models.BadCaseModel.message_id == message_id
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def count(self) -> int:
        """点踩总数（FR11 采纳率分母）。"""
        async with self._sf() as session:
            stmt = select(func.count()).select_from(models.BadCaseModel)
            return int((await session.execute(stmt)).scalar() or 0)


class SqlDomainMemoryRepository:
    """领域知识记忆（L3）的 MySQL 实现；few_shot_examples 视为其已验证样本视图。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    def _to_data(self, r: models.DomainMemoryModel) -> DomainMemoryData:
        return DomainMemoryData(
            id=r.id,
            domain=r.domain,
            type=r.type,
            question=r.question,
            answer=r.answer,
            intent=r.intent,
            tags=r.tags,
            weight=float(r.weight),
            status=r.status,
            created_by=r.created_by,
            created_at=r.created_at,
        )

    async def add(self, item: DomainMemoryData) -> DomainMemoryData:
        async with self._sf() as session:
            obj = models.DomainMemoryModel(
                domain=item.domain,
                type=item.type,
                question=item.question,
                answer=item.answer,
                intent=item.intent,
                tags=item.tags,
                weight=item.weight,
                status=item.status,
                created_by=item.created_by,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def list_all(self) -> List[DomainMemoryData]:
        async with self._sf() as session:
            stmt = select(models.DomainMemoryModel).order_by(
                models.DomainMemoryModel.created_at.desc()
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    def _conditions(
        self,
        *,
        domain: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Any]:
        conds: List[Any] = []
        if domain:
            conds.append(models.DomainMemoryModel.domain == domain)
        if type:
            conds.append(models.DomainMemoryModel.type == type)
        if status:
            conds.append(models.DomainMemoryModel.status == status)
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            conds.append(
                or_(
                    models.DomainMemoryModel.question.like(like),
                    models.DomainMemoryModel.answer.like(like),
                    models.DomainMemoryModel.tags.like(like),
                )
            )
        return conds

    async def query(
        self,
        *,
        domain: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[DomainMemoryData], int]:
        conds = self._conditions(
            domain=domain, type=type, status=status, keyword=keyword
        )
        async with self._sf() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(models.DomainMemoryModel)
                        .where(*conds)
                    )
                ).scalar()
                or 0
            )
            stmt = (
                select(models.DomainMemoryModel)
                .where(*conds)
                .order_by(models.DomainMemoryModel.created_at.desc())
                .limit(max(limit, 1))
                .offset(max(offset, 0))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows], total

    async def update(
        self,
        item_id: int,
        *,
        domain: Optional[str] = None,
        type: Optional[str] = None,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        intent: Optional[str] = None,
        tags: Optional[str] = None,
        weight: Optional[float] = None,
        status: Optional[str] = None,
    ) -> Optional[DomainMemoryData]:
        fields: Dict[str, Any] = {}
        for name, value in (
            ("domain", domain), ("type", type), ("question", question),
            ("answer", answer), ("intent", intent), ("tags", tags),
            ("weight", weight), ("status", status),
        ):
            if value is not None:
                fields[name] = value
        async with self._sf() as session:
            obj = await session.get(models.DomainMemoryModel, item_id)
            if obj is None:
                return None
            for name, value in fields.items():
                setattr(obj, name, value)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def count_by_type(self) -> Dict[str, int]:
        async with self._sf() as session:
            stmt = (
                select(
                    models.DomainMemoryModel.type,
                    func.count(),
                )
                .group_by(models.DomainMemoryModel.type)
            )
            rows = (await session.execute(stmt)).all()
            return {str(t or ""): int(c or 0) for t, c in rows}

    async def list_approved(
        self, domain: Optional[str] = None, type: Optional[str] = None
    ) -> List[DomainMemoryData]:
        async with self._sf() as session:
            stmt = select(models.DomainMemoryModel).where(
                models.DomainMemoryModel.status == "approved"
            )
            if domain:
                stmt = stmt.where(models.DomainMemoryModel.domain == domain)
            if type:
                stmt = stmt.where(models.DomainMemoryModel.type == type)
            stmt = stmt.order_by(models.DomainMemoryModel.weight.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_data(r) for r in rows]

    async def get_by_id(self, item_id: int) -> Optional[DomainMemoryData]:
        async with self._sf() as session:
            obj = await session.get(models.DomainMemoryModel, item_id)
            return self._to_data(obj) if obj else None

    async def delete(self, item_id: int) -> None:
        async with self._sf() as session:
            obj = await session.get(models.DomainMemoryModel, item_id)
            if obj is not None:
                await session.delete(obj)
                await session.commit()


def _to_tenant_data(m: models.TenantModel) -> TenantData:
    return TenantData(
        id=m.id, code=m.code, name=m.name, status=m.status, created_at=m.created_at
    )


def _to_user_data(m: models.UserModel) -> UserData:
    return UserData(
        id=m.id, username=m.username, display_name=m.display_name or "",
        tenant_id=m.tenant_id, role=m.role, status=m.status,
        created_at=m.created_at, updated_at=m.updated_at, last_login_at=m.last_login_at,
    )


class SqlTenantRepository:
    """租户仓储（MySQL）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, tenant: TenantData) -> TenantData:
        obj = models.TenantModel(
            id=tenant.id or _new_id(),
            code=tenant.code,
            name=tenant.name,
            status=tenant.status or "active",
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return _to_tenant_data(obj)

    async def get(self, tenant_id: str) -> Optional[TenantData]:
        async with self._sf() as session:
            obj = await session.get(models.TenantModel, tenant_id)
            return _to_tenant_data(obj) if obj else None

    async def get_by_code(self, code: str) -> Optional[TenantData]:
        async with self._sf() as session:
            stmt = select(models.TenantModel).where(models.TenantModel.code == code)
            obj = (await session.execute(stmt)).scalars().first()
            return _to_tenant_data(obj) if obj else None

    async def list_all(self) -> List[TenantData]:
        async with self._sf() as session:
            stmt = select(models.TenantModel).order_by(models.TenantModel.created_at.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_tenant_data(r) for r in rows]

    async def update(
        self, tenant_id: str, name: Optional[str] = None, status: Optional[str] = None
    ) -> Optional[TenantData]:
        values: Dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if status is not None:
            values["status"] = status
        if values:
            async with self._sf() as session:
                await session.execute(
                    update(models.TenantModel)
                    .where(models.TenantModel.id == tenant_id)
                    .values(**values)
                )
                await session.commit()
        return await self.get(tenant_id)

    async def delete(self, tenant_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.TenantModel, tenant_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True


class SqlUserRepository:
    """用户仓储（MySQL；口令以哈希存储）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, user: UserData, password_hash: str) -> UserData:
        obj = models.UserModel(
            id=user.id or _new_id(),
            username=user.username,
            password_hash=password_hash,
            display_name=user.display_name or "",
            tenant_id=user.tenant_id,
            role=user.role or "viewer",
            status=user.status or "active",
        )
        async with self._sf() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            user.id = obj.id
            return _to_user_data(obj)

    async def get(self, user_id: str) -> Optional[UserData]:
        async with self._sf() as session:
            obj = await session.get(models.UserModel, user_id)
            return _to_user_data(obj) if obj else None

    async def get_by_username(self, username: str) -> Optional[UserData]:
        async with self._sf() as session:
            stmt = select(models.UserModel).where(models.UserModel.username == username)
            obj = (await session.execute(stmt)).scalars().first()
            return _to_user_data(obj) if obj else None

    async def get_password_hash(self, user_id: str) -> Optional[str]:
        async with self._sf() as session:
            obj = await session.get(models.UserModel, user_id)
            return obj.password_hash if obj else None

    async def list_all(self, tenant_id: Optional[str] = None) -> List[UserData]:
        async with self._sf() as session:
            stmt = select(models.UserModel).order_by(models.UserModel.created_at.asc())
            if tenant_id:
                stmt = stmt.where(models.UserModel.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_user_data(r) for r in rows]

    async def update(
        self,
        user_id: str,
        display_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[UserData]:
        values: Dict[str, Any] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if tenant_id is not None:
            values["tenant_id"] = tenant_id
        if role is not None:
            values["role"] = role
        if status is not None:
            values["status"] = status
        if values:
            async with self._sf() as session:
                await session.execute(
                    update(models.UserModel)
                    .where(models.UserModel.id == user_id)
                    .values(**values)
                )
                await session.commit()
        return await self.get(user_id)

    async def set_password(self, user_id: str, password_hash: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.UserModel, user_id)
            if obj is None:
                return False
            obj.password_hash = password_hash
            await session.commit()
            return True

    async def touch_login(self, user_id: str) -> None:
        async with self._sf() as session:
            await session.execute(
                update(models.UserModel)
                .where(models.UserModel.id == user_id)
                .values(last_login_at=func.now())
            )
            await session.commit()

    async def delete(self, user_id: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.UserModel, user_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def count(self) -> int:
        async with self._sf() as session:
            stmt = select(func.count()).select_from(models.UserModel)
            return int((await session.execute(stmt)).scalar() or 0)


def _to_preference_data(m: models.UserPreferenceModel) -> UserPreferenceData:
    return UserPreferenceData(
        id=m.id,
        user_id=m.user_id,
        tenant_id=m.tenant_id or "",
        key=m.pref_key,
        value=m.value or "",
        source=m.source or "user",
        confidence=float(m.confidence if m.confidence is not None else 1.0),
        status=m.status or "active",
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SqlUserPreferenceRepository:
    """用户偏好（L2）SQL 实现：按 (user_id, pref_key) upsert。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def upsert(self, pref: UserPreferenceData) -> UserPreferenceData:
        async with self._sf() as session:
            stmt = select(models.UserPreferenceModel).where(
                models.UserPreferenceModel.user_id == pref.user_id,
                models.UserPreferenceModel.pref_key == pref.key,
            )
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                obj = models.UserPreferenceModel(
                    user_id=pref.user_id, pref_key=pref.key
                )
                session.add(obj)
            obj.tenant_id = pref.tenant_id
            obj.value = pref.value
            obj.source = pref.source
            obj.confidence = pref.confidence
            obj.status = pref.status
            await session.commit()
            await session.refresh(obj)
            return _to_preference_data(obj)

    async def list_by_user(
        self, user_id: str, status: Optional[str] = None
    ) -> List[UserPreferenceData]:
        async with self._sf() as session:
            stmt = (
                select(models.UserPreferenceModel)
                .where(models.UserPreferenceModel.user_id == user_id)
                .order_by(models.UserPreferenceModel.updated_at.desc())
            )
            if status:
                stmt = stmt.where(models.UserPreferenceModel.status == status)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_preference_data(r) for r in rows]

    async def list_all(
        self,
        *,
        tenant_id: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[UserPreferenceData]:
        """全平台偏好（管理面 L2 视角）；调用方须有 memory 权限点（否则越权读他人偏好）。"""
        async with self._sf() as session:
            stmt = select(models.UserPreferenceModel)
            if tenant_id:
                stmt = stmt.where(models.UserPreferenceModel.tenant_id == tenant_id)
            if source:
                stmt = stmt.where(models.UserPreferenceModel.source == source)
            if status:
                stmt = stmt.where(models.UserPreferenceModel.status == status)
            if user_id:
                stmt = stmt.where(models.UserPreferenceModel.user_id == user_id)
            stmt = stmt.order_by(models.UserPreferenceModel.updated_at.desc()).limit(
                max(limit, 1)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_preference_data(r) for r in rows]

    async def get(self, user_id: str, key: str) -> Optional[UserPreferenceData]:
        async with self._sf() as session:
            stmt = select(models.UserPreferenceModel).where(
                models.UserPreferenceModel.user_id == user_id,
                models.UserPreferenceModel.pref_key == key,
            )
            obj = (await session.execute(stmt)).scalars().first()
            return _to_preference_data(obj) if obj else None

    async def delete(self, user_id: str, key: str) -> bool:
        async with self._sf() as session:
            stmt = select(models.UserPreferenceModel).where(
                models.UserPreferenceModel.user_id == user_id,
                models.UserPreferenceModel.pref_key == key,
            )
            obj = (await session.execute(stmt)).scalars().first()
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def delete_by_id(self, pref_id: int) -> bool:
        """按主键删除（管理面跨用户删除；本人接口仍走 delete(user_id, key)）。"""
        async with self._sf() as session:
            obj = await session.get(models.UserPreferenceModel, pref_id)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def clear_user(self, user_id: str) -> int:
        async with self._sf() as session:
            stmt = delete(models.UserPreferenceModel).where(
                models.UserPreferenceModel.user_id == user_id
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(models.UserPreferenceModel)
                    )
                ).scalar()
                or 0
            )


class SqlSemanticRegistryRepository:
    """语义注册中心 MySQL 实现（P1：只读 + 幂等种子导入）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ---------- 租户作用域谓词 ----------
    #
    # 注册中心几张表的租户归属并不一致：semantic_models / semantic_metrics 自带 tenant_id，
    # 而 semantic_dimensions / semantic_measures 只挂 model_id —— 维度与度量的租户归属
    # **继承宿主模型**。所以谓词分两组，别在每个方法里各写各的。
    #
    # 两套语义刻意分开（读得到 ≠ 改得动）：
    #   - 可见性（读）：None = 不过滤（系统/全量视角）｜"" = 仅全局｜"t" = 全局 + 本租户；
    #   - 归属（写 / 按 key 精确定位）：None = 历史行为（任意行）｜否则**精确等于**该租户。
    #     "精确等于"是安全的关键 —— 全局条目 tenant_id=""，租户传自己的 id 时匹配不到，
    #     于是"租户改到了全局定义"在仓储层就变成定位失败（上层 404），
    #     而不是指望每个调用点都记得校验。

    @staticmethod
    def _visible_tenant_clause(column, tenant_id: Optional[str]):
        """读取可见性谓词：租户看得到全局（只读）与自己名下。"""
        if tenant_id is None:
            return None
        tid = (tenant_id or "").strip()
        if not tid:
            return column == ""
        return column.in_(("", tid))

    @staticmethod
    def _owner_tenant_clause(column, tenant_id: Optional[str]):
        """写入 / 精确定位谓词：必须**恰好**属于该租户（全局条目对租户不可写）。"""
        if tenant_id is None:
            return None
        return column == (tenant_id or "").strip()

    @staticmethod
    def _visible_model_ids_subquery(tenant_id: str):
        sub = select(models.SemanticModelModel.id)
        clause = SqlSemanticRegistryRepository._visible_tenant_clause(
            models.SemanticModelModel.tenant_id, tenant_id
        )
        return sub.where(clause) if clause is not None else sub

    @staticmethod
    def _owner_model_ids_subquery(tenant_id: str):
        return select(models.SemanticModelModel.id).where(
            models.SemanticModelModel.tenant_id == (tenant_id or "").strip()
        )

    @staticmethod
    def _visible_dimension_clause(tenant_id: Optional[str]):
        """维度可见性：经宿主模型收敛（半连接子查询）。"""
        if tenant_id is None:
            return None
        return models.SemanticDimensionModel.model_id.in_(
            SqlSemanticRegistryRepository._visible_model_ids_subquery(tenant_id)
        )

    @staticmethod
    def _visible_measure_clause(tenant_id: Optional[str]):
        if tenant_id is None:
            return None
        return models.SemanticMeasureModel.model_id.in_(
            SqlSemanticRegistryRepository._visible_model_ids_subquery(tenant_id)
        )

    async def _metric_row(
        self, session, metric_key: str, tenant_id: Optional[str]
    ):
        """按 (metric_key, 租户归属) 精确定位一行指标；定位不到返回 None。"""
        stmt = select(models.SemanticMetricModel).where(
            models.SemanticMetricModel.metric_key == metric_key
        )
        clause = self._owner_tenant_clause(
            models.SemanticMetricModel.tenant_id, tenant_id
        )
        if clause is not None:
            stmt = stmt.where(clause)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _model_row(
        self, session, model_key: str, tenant_id: Optional[str]
    ):
        """按 (model_key, 租户归属) 精确定位一行模型；定位不到返回 None。"""
        stmt = select(models.SemanticModelModel).where(
            models.SemanticModelModel.model_key == model_key
        )
        clause = self._owner_tenant_clause(
            models.SemanticModelModel.tenant_id, tenant_id
        )
        if clause is not None:
            stmt = stmt.where(clause)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _dimension_row(
        self, session, dimension_key: str, tenant_id: Optional[str]
    ):
        """按 (dimension_key, 宿主模型租户归属) 精确定位一行维度。

        维度 key 只保证"模型内唯一"（uk_sem_dimension 是 (model_id, dimension_key)），
        所以带租户时必须同时收窄到该租户名下的模型；否则同一个 key 可能命中多个模型行，
        ``scalar_one_or_none`` 会直接抛 MultipleResultsFound。
        """
        stmt = select(models.SemanticDimensionModel).where(
            models.SemanticDimensionModel.dimension_key == dimension_key
        )
        if tenant_id is not None:
            stmt = stmt.where(
                models.SemanticDimensionModel.model_id.in_(
                    self._owner_model_ids_subquery(tenant_id)
                )
            )
        return (await session.execute(stmt)).scalar_one_or_none()

    # ---------- 内部工具 ----------

    @staticmethod
    def _model_brief(r: models.SemanticModelModel, dims: int, measures: int) -> Dict[str, Any]:
        return {
            "model_key": r.model_key,
            "name": r.name,
            "description": r.description or "",
            "table": r.table,
            "agg_time_dimension": r.agg_time_dimension or "",
            "dimension_count": dims,
            "measure_count": measures,
            "status": r.status,
            "version": r.version,
            "source": r.source,
            "owner": r.owner or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

    @classmethod
    def _model_detail(cls, r: models.SemanticModelModel, dims, measures) -> Dict[str, Any]:
        payload = cls._model_brief(r, len(dims), len(measures))
        payload.pop("dimension_count", None)
        payload.pop("measure_count", None)
        payload["entities"] = list(r.entities or [])
        payload["dimensions"] = [
            {
                "dimension_key": d.dimension_key,
                "name": d.name,
                "dim_type": d.dim_type,
                "expr": d.expr,
                "time_granularity": d.time_granularity,
                "description": d.description or "",
                "status": d.status,
            }
            for d in dims
        ]
        payload["measures"] = [
            {
                "measure_key": x.measure_key,
                "name": x.name,
                "agg": x.agg,
                "expr": x.expr,
                "agg_time_dimension": x.agg_time_dimension or "",
                "unit": x.unit or "",
                "description": x.description or "",
                "status": x.status,
            }
            for x in measures
        ]
        return payload

    @staticmethod
    def _metric_brief(r: models.SemanticMetricModel) -> Dict[str, Any]:
        return {
            "id": r.id,
            "metric_key": r.metric_key,
            "name": r.name,
            "metric_type": r.metric_type,
            "type_params": dict(r.type_params or {}),
            "aliases": list(r.aliases or []),
            "unit": r.unit or "",
            "description": r.description or "",
            "scale": float(r.scale or 1.0),
            "round": int(r.round or 4),
            "granularities": list(r.granularities or []),
            "pop_types": list(r.pop_types or []),
            "status": r.status,
            "version": r.version,
            "source": r.source,
            "owner": r.owner or "",
            "approved_by": r.approved_by or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

    # ---------- 列表 / 详情 ----------

    async def list_models(
        self,
        *,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            stmt = select(models.SemanticModelModel)
            clause = self._visible_tenant_clause(
                models.SemanticModelModel.tenant_id, tenant_id
            )
            if clause is not None:
                stmt = stmt.where(clause)
            if status:
                stmt = stmt.where(models.SemanticModelModel.status == status)
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        models.SemanticModelModel.model_key.like(kw),
                        models.SemanticModelModel.name.like(kw),
                        models.SemanticModelModel.description.like(kw),
                    )
                )
            total = (
                await session.execute(
                    select(func.count()).select_from(stmt.subquery())
                )
            ).scalar() or 0
            rows = (
                await session.execute(stmt.order_by(models.SemanticModelModel.id).limit(limit).offset(offset))
            ).scalars().all()
            ids = [r.id for r in rows]
            dim_counts = await session.execute(
                select(
                    models.SemanticDimensionModel.model_id, func.count()
                ).where(models.SemanticDimensionModel.model_id.in_(ids or [0])).group_by(
                    models.SemanticDimensionModel.model_id
                )
            )
            measure_counts = await session.execute(
                select(
                    models.SemanticMeasureModel.model_id, func.count()
                ).where(models.SemanticMeasureModel.model_id.in_(ids or [0])).group_by(
                    models.SemanticMeasureModel.model_id
                )
            )
            dc = dict(dim_counts.all())
            mc = dict(measure_counts.all())
            items = [self._model_brief(r, dc.get(r.id, 0), mc.get(r.id, 0)) for r in rows]
            return items, int(total)

    async def get_model(
        self, model_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            # 详情用**可见性**而非归属：租户要能打开全局模型看它的维度/度量（只读），
            # 但同一 key 若本租户自己也有，优先给本租户那份（先精确、再回落全局）。
            row = (
                await self._model_row(session, model_key, tenant_id)
                if tenant_id is not None
                else await self._model_row(session, model_key, None)
            )
            if row is None and tenant_id:
                row = await self._model_row(session, model_key, "")
            if row is None:
                return None
            dims = (
                await session.execute(
                    select(models.SemanticDimensionModel)
                    .where(models.SemanticDimensionModel.model_id == row.id)
                    .order_by(models.SemanticDimensionModel.id)
                )
            ).scalars().all()
            measures = (
                await session.execute(
                    select(models.SemanticMeasureModel)
                    .where(models.SemanticMeasureModel.model_id == row.id)
                    .order_by(models.SemanticMeasureModel.id)
                )
            ).scalars().all()
            return self._model_detail(row, dims, measures)

    async def list_dimensions(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            model_id = None
            if model_key:
                # 指定模型时同样遵守"先本租户、再回落全局"，与 get_model 一致
                m = await self._model_row(session, model_key, tenant_id)
                if m is None and tenant_id:
                    m = await self._model_row(session, model_key, "")
                if m is None:
                    return [], 0
                model_id = m.id
            stmt = select(models.SemanticDimensionModel)
            clause = self._visible_dimension_clause(tenant_id)
            if clause is not None:
                stmt = stmt.where(clause)
            if model_id is not None:
                stmt = stmt.where(models.SemanticDimensionModel.model_id == model_id)
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        models.SemanticDimensionModel.name.like(kw),
                        models.SemanticDimensionModel.dimension_key.like(kw),
                    )
                )
            total = (
                await session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar() or 0
            rows = (
                await session.execute(
                    stmt.order_by(models.SemanticDimensionModel.model_id, models.SemanticDimensionModel.id).limit(limit).offset(offset)
                )
            ).scalars().all()
            model_ids = {r.model_id for r in rows}
            key_by_id = dict(
                (
                    await session.execute(
                        select(models.SemanticModelModel.id, models.SemanticModelModel.model_key).where(
                            models.SemanticModelModel.id.in_(model_ids or [0])
                        )
                    )
                ).all()
            )
            items = [
                {
                    "dimension_key": r.dimension_key,
                    "name": r.name,
                    "dim_type": r.dim_type,
                    "expr": r.expr,
                    "time_granularity": r.time_granularity,
                    "description": r.description or "",
                    "status": r.status,
                    "model_key": key_by_id.get(r.model_id, ""),
                }
                for r in rows
            ]
            return items, int(total)

    async def list_measures(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            model_id = None
            if model_key:
                m = await self._model_row(session, model_key, tenant_id)
                if m is None and tenant_id:
                    m = await self._model_row(session, model_key, "")
                if m is None:
                    return [], 0
                model_id = m.id
            stmt = select(models.SemanticMeasureModel)
            clause = self._visible_measure_clause(tenant_id)
            if clause is not None:
                stmt = stmt.where(clause)
            if model_id is not None:
                stmt = stmt.where(models.SemanticMeasureModel.model_id == model_id)
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        models.SemanticMeasureModel.name.like(kw),
                        models.SemanticMeasureModel.measure_key.like(kw),
                    )
                )
            total = (
                await session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar() or 0
            rows = (
                await session.execute(
                    stmt.order_by(models.SemanticMeasureModel.model_id, models.SemanticMeasureModel.id).limit(limit).offset(offset)
                )
            ).scalars().all()
            model_ids = {r.model_id for r in rows}
            key_by_id = dict(
                (
                    await session.execute(
                        select(models.SemanticModelModel.id, models.SemanticModelModel.model_key).where(
                            models.SemanticModelModel.id.in_(model_ids or [0])
                        )
                    )
                ).all()
            )
            items = [
                {
                    "measure_key": r.measure_key,
                    "name": r.name,
                    "agg": r.agg,
                    "expr": r.expr,
                    "agg_time_dimension": r.agg_time_dimension or "",
                    "unit": r.unit or "",
                    "description": r.description or "",
                    "status": r.status,
                    "model_key": key_by_id.get(r.model_id, ""),
                }
                for r in rows
            ]
            return items, int(total)

    async def list_metrics(
        self,
        *,
        status: Optional[str] = None,
        metric_type: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            stmt = select(models.SemanticMetricModel)
            clause = self._visible_tenant_clause(
                models.SemanticMetricModel.tenant_id, tenant_id
            )
            if clause is not None:
                stmt = stmt.where(clause)
            if status:
                stmt = stmt.where(models.SemanticMetricModel.status == status)
            if metric_type:
                stmt = stmt.where(models.SemanticMetricModel.metric_type == metric_type)
            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    or_(
                        models.SemanticMetricModel.name.like(kw),
                        models.SemanticMetricModel.metric_key.like(kw),
                        models.SemanticMetricModel.aliases.like(kw),
                    )
                )
            total = (
                await session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar() or 0
            rows = (
                await session.execute(
                    stmt.order_by(models.SemanticMetricModel.id).limit(limit).offset(offset)
                )
            ).scalars().all()
            return [self._metric_brief(r) for r in rows], int(total)

    async def get_metric(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            # 与 get_model 同口径：详情按"可见性"取，先本租户、再回落全局（只读全局）
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None and tenant_id:
                row = await self._metric_row(session, metric_key, "")
            if row is None:
                return None
            payload = self._metric_brief(row)
            # 宿主模型 + 可用维度（P1 信息性展示；血缘图 P2 补充）
            hosts: List[Dict[str, Any]] = []
            if row.metric_type == "simple":
                m = (
                    await session.execute(
                        select(models.SemanticModelModel)
                        .join(
                            models.SemanticMeasureModel,
                            models.SemanticMeasureModel.model_id == models.SemanticModelModel.id,
                        )
                        .where(models.SemanticMeasureModel.measure_key == (row.type_params or {}).get("measure"))
                    )
                ).scalars().first()
                if m is not None:
                    hosts.append({"model_key": m.model_key, "name": m.name})
            payload["host_models"] = hosts
            return payload

    # ---------- 统计 / facets ----------

    async def stats(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """统计卡；``tenant_id`` 非 None 时按可见性收敛（全局 + 本租户）。"""
        m_clause = self._visible_tenant_clause(
            models.SemanticModelModel.tenant_id, tenant_id
        )
        k_clause = self._visible_tenant_clause(
            models.SemanticMetricModel.tenant_id, tenant_id
        )
        d_clause = self._visible_dimension_clause(tenant_id)
        s_clause = self._visible_measure_clause(tenant_id)
        async with self._sf() as session:

            def _scoped(stmt, clause):
                return stmt.where(clause) if clause is not None else stmt

            def _group(column, clause):
                return _scoped(select(column, func.count()), clause).group_by(column)

            async def _count(model, clause) -> int:
                return int(
                    (
                        await session.execute(_scoped(select(func.count()).select_from(model), clause))
                    ).scalar()
                    or 0
                )

            metrics_by_status = dict(
                (await session.execute(_group(models.SemanticMetricModel.status, k_clause))).all()
            )
            metrics_by_type = dict(
                (await session.execute(_group(models.SemanticMetricModel.metric_type, k_clause))).all()
            )
            models_by_status = dict(
                (await session.execute(_group(models.SemanticModelModel.status, m_clause))).all()
            )
            return {
                "models": await _count(models.SemanticModelModel, m_clause),
                "metrics": await _count(models.SemanticMetricModel, k_clause),
                "dimensions": await _count(models.SemanticDimensionModel, d_clause),
                "measures": await _count(models.SemanticMeasureModel, s_clause),
                "metrics_by_status": metrics_by_status,
                "metrics_by_type": metrics_by_type,
                "models_by_status": models_by_status,
            }

    async def facets(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        k_clause = self._visible_tenant_clause(
            models.SemanticMetricModel.tenant_id, tenant_id
        )
        m_clause = self._visible_tenant_clause(
            models.SemanticModelModel.tenant_id, tenant_id
        )
        async with self._sf() as session:

            def _scoped(stmt, clause):
                return stmt.where(clause) if clause is not None else stmt

            types = [
                r[0]
                for r in (
                    await session.execute(
                        _scoped(select(models.SemanticMetricModel.metric_type).distinct(), k_clause)
                    )
                ).all()
            ]
            statuses = [
                r[0]
                for r in (
                    await session.execute(
                        _scoped(select(models.SemanticMetricModel.status).distinct(), k_clause)
                    )
                ).all()
            ]
            model_keys = [
                r[0]
                for r in (
                    await session.execute(
                        _scoped(
                            select(models.SemanticModelModel.model_key).order_by(
                                models.SemanticModelModel.id
                            ),
                            m_clause,
                        )
                    )
                ).all()
            ]
            units = [
                r[0]
                for r in (
                    await session.execute(
                        _scoped(
                            select(models.SemanticMetricModel.unit).distinct().where(
                                models.SemanticMetricModel.unit != ""
                            ),
                            k_clause,
                        )
                    )
                ).all()
            ]
            return {
                "metric_types": types,
                "statuses": statuses,
                "model_keys": model_keys,
                "units": units,
            }

    # ---------- 种子导入（幂等） ----------

    async def import_seed(
        self, seed: Dict[str, Any], *, operator: str = "system"
    ) -> Dict[str, Any]:
        models_imported = models_skipped = metrics_imported = metrics_skipped = 0
        async with self._sf() as session:
            for raw in seed.get("models") or []:
                key = raw.get("model_key") or ""
                if not key:
                    continue
                exists = (
                    await session.execute(
                        select(func.count()).select_from(models.SemanticModelModel).where(
                            models.SemanticModelModel.model_key == key
                        )
                    )
                ).scalar() or 0
                if exists:
                    models_skipped += 1
                    continue
                m = models.SemanticModelModel(
                    scope=raw.get("scope") or "industry",
                    model_key=key,
                    name=raw.get("name") or key,
                    description=raw.get("description") or "",
                    table=raw.get("table") or key,
                    agg_time_dimension=raw.get("agg_time_dimension") or "",
                    entities=raw.get("entities") or [],
                    status=raw.get("status") or "published",
                    source=raw.get("source") or "builtin",
                    created_by=operator,
                )
                session.add(m)
                await session.flush()
                for d in raw.get("dimensions") or []:
                    session.add(
                        models.SemanticDimensionModel(
                            model_id=m.id,
                            dimension_key=d.get("dimension_key") or d.get("name") or "",
                            name=d.get("name") or "",
                            dim_type=d.get("dim_type") or "categorical",
                            expr=d.get("expr") or "",
                            time_granularity=d.get("time_granularity") or "day",
                            description=d.get("description") or "",
                        )
                    )
                for x in raw.get("measures") or []:
                    session.add(
                        models.SemanticMeasureModel(
                            model_id=m.id,
                            measure_key=x.get("measure_key") or x.get("name") or "",
                            name=x.get("name") or "",
                            agg=x.get("agg") or "sum",
                            expr=x.get("expr") or "",
                            agg_time_dimension=x.get("agg_time_dimension") or "",
                            unit=x.get("unit") or "",
                            description=x.get("description") or "",
                        )
                    )
                models_imported += 1

            for raw in seed.get("metrics") or []:
                key = raw.get("metric_key") or ""
                if not key:
                    continue
                exists = (
                    await session.execute(
                        select(func.count()).select_from(models.SemanticMetricModel).where(
                            models.SemanticMetricModel.metric_key == key
                        )
                    )
                ).scalar() or 0
                if exists:
                    metrics_skipped += 1
                    continue
                row = models.SemanticMetricModel(
                    scope=raw.get("scope") or "industry",
                    metric_key=key,
                    name=raw.get("name") or key,
                    metric_type=raw.get("metric_type") or "simple",
                    type_params=raw.get("type_params") or {},
                    aliases=raw.get("aliases") or [],
                    unit=raw.get("unit") or "",
                    description=raw.get("description") or "",
                    scale=float(raw.get("scale") or 1.0),
                    round=int(raw.get("round") or 4),
                    granularities=raw.get("granularities") or [],
                    pop_types=raw.get("pop_types") or [],
                    status=raw.get("status") or "published",
                    source=raw.get("source") or "builtin",
                    created_by=operator,
                )
                session.add(row)
                await session.flush()
                # 版本快照直接取种子 payload（flush 后 ORM 属性如 created_at 可能
                # 触发隐式刷新 IO -> MissingGreenlet，故不经 _metric_brief(row)）
                session.add(
                    models.SemanticMetricVersionModel(
                        metric_id=row.id,
                        version=1,
                        change_type="import",
                        after_json={**raw, "id": row.id, "created_by": operator},
                        operator=operator,
                    )
                )
                metrics_imported += 1
            # 单事务提交：模型 + 指标要么全量成功，要么整体回滚
            # （拆两段 commit 曾导致"模型进了、指标没进"的半截状态）。
            await session.commit()
        return {
            "models_imported": models_imported,
            "models_skipped": models_skipped,
            "metrics_imported": metrics_imported,
            "metrics_skipped": metrics_skipped,
        }

    async def count_models(self) -> int:
        async with self._sf() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(models.SemanticModelModel)
                    )
                ).scalar()
                or 0
            )


    # ---------- 写路径：指标（P2 版本与口径审批）----------

    def _metric_brief_from(
        self, defn, *, id, metric_key, status, version, source, operator, created_at=None
    ) -> Dict[str, Any]:
        return {
            "id": id,
            "metric_key": metric_key,
            "name": defn.get("name") or metric_key,
            "metric_type": defn.get("metric_type") or "simple",
            "type_params": dict(defn.get("type_params") or {}),
            "aliases": list(defn.get("aliases") or []),
            "unit": defn.get("unit") or "",
            "description": defn.get("description") or "",
            "scale": float(defn.get("scale") or 1.0),
            "round": int(defn.get("round") or 4),
            "granularities": list(defn.get("granularities") or []),
            "pop_types": list(defn.get("pop_types") or []),
            "status": status,
            "version": version,
            "source": source,
            "owner": defn.get("owner") or "",
            "created_at": created_at,
            "updated_at": created_at,
        }

    async def _gather_metric_defs(
        self,
        session,
        *,
        include_key: Optional[str] = None,
        include_defn: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """发布校验用的引用域：全局 ∪ 本租户（可见性），且租户同名条目覆盖全局。"""
        stmt = select(models.SemanticMetricModel)
        clause = self._visible_tenant_clause(
            models.SemanticMetricModel.tenant_id, tenant_id
        )
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = stmt.order_by(
            models.SemanticMetricModel.tenant_id, models.SemanticMetricModel.id
        )
        rows = (await session.execute(stmt)).scalars().all()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            b = self._metric_brief(r)
            out[r.metric_key] = {
                "metric_type": b["metric_type"],
                "type_params": b["type_params"],
                "status": b["status"],
            }
        if include_key is not None and include_defn is not None:
            out[include_key] = {
                "metric_type": include_defn["metric_type"],
                "type_params": include_defn["type_params"],
                "status": include_defn.get("status", DRAFT),
            }
        return out

    async def _gather_measure_keys(
        self, session, *, tenant_id: Optional[str] = None
    ) -> Set[str]:
        stmt = select(models.SemanticMeasureModel.measure_key)
        clause = self._visible_measure_clause(tenant_id)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = (await session.execute(stmt)).scalars().all()
        return set(rows)

    async def create_metric_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """起草指标。``tenant_id`` 非 None 时**强制覆盖**载荷里的归属 —— 租户归属不该由
        请求体自己说了算（否则"我在 body 里写 tenant_id='' 就能改全局"）。"""
        async with self._sf() as session:
            scope = defn.get("scope") or "industry"
            tenant_id = (
                (tenant_id or "").strip()
                if tenant_id is not None
                else (defn.get("tenant_id") or "")
            )
            key = defn.get("metric_key") or derive_metric_key(defn)
            base, n = key, 2
            while True:
                exists = (
                    await session.execute(
                        select(func.count())
                        .select_from(models.SemanticMetricModel)
                        .where(
                            models.SemanticMetricModel.scope == scope,
                            models.SemanticMetricModel.tenant_id == tenant_id,
                            models.SemanticMetricModel.metric_key == key,
                        )
                    )
                ).scalar() or 0
                if not exists:
                    break
                key = f"{base}_{n}"
                n += 1
            row = models.SemanticMetricModel(
                scope=scope,
                tenant_id=tenant_id,
                metric_key=key,
                name=defn.get("name") or key,
                metric_type=defn.get("metric_type") or "simple",
                type_params=defn.get("type_params") or {},
                aliases=defn.get("aliases") or [],
                unit=defn.get("unit") or "",
                description=defn.get("description") or "",
                scale=float(defn.get("scale") or 1.0),
                round=int(defn.get("round") or 4),
                granularities=defn.get("granularities") or [],
                pop_types=defn.get("pop_types") or [],
                owner=defn.get("owner") or "",
                status=DRAFT,
                version=1,
                source="user",
                created_by=operator,
                updated_by=operator,
            )
            session.add(row)
            await session.flush()
            core = metric_core_defn(
                self._metric_brief_from(
                    defn, id=row.id, metric_key=key, status=DRAFT,
                    version=1, source="user", operator=operator,
                )
            )
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=1, change_type="create",
                    after_json=core, operator=operator,
                )
            )
            await session.commit()
            return self._metric_brief_from(
                defn, id=row.id, metric_key=key, status=DRAFT,
                version=1, source="user", operator=operator,
                created_at=datetime.now().isoformat(),
            )

    async def update_metric(
        self,
        metric_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None:
                return None
            if row.status not in (DRAFT, REVIEW):
                raise ValueError(
                    f"只能编辑草稿/待审指标（当前 {row.status}）；"
                    f"已发布/已废弃请先废弃或基于新版本重起草"
                )
            before = metric_core_defn(self._metric_brief(row))
            row.name = defn.get("name") or row.name
            row.metric_type = defn.get("metric_type") or row.metric_type
            row.type_params = defn.get("type_params") or row.type_params
            row.aliases = defn.get("aliases") or row.aliases
            row.unit = defn.get("unit") or ""
            row.description = defn.get("description") or ""
            row.scale = float(defn.get("scale") or row.scale or 1.0)
            row.round = int(defn.get("round") or row.round or 4)
            row.granularities = defn.get("granularities") or row.granularities
            row.pop_types = defn.get("pop_types") or row.pop_types
            row.owner = defn.get("owner") or row.owner
            row.updated_by = operator
            after = metric_core_defn(self._metric_brief(row))
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=row.version, change_type="update",
                    before_json=before, after_json=after, operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._metric_brief(row)

    async def submit_metric_review(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None:
                return None
            if not can_transition(row.status, REVIEW):
                raise ValueError(f"非法状态转移：{row.status} -> {REVIEW}")
            row.status = REVIEW
            row.updated_by = operator
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=row.version, change_type="submit",
                    after_json=metric_core_defn(self._metric_brief(row)),
                    operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._metric_brief(row)

    async def publish_metric(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        approved_by: str = "",
        layer: Any = None,
        engine: Any = None,
        deep: bool = False,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None:
                return {"ok": False, "error": "not_found"}
            if not can_transition(row.status, PUBLISHED):
                return {"ok": False, "error": "illegal_transition", "current": row.status}
            defn = metric_core_defn(self._metric_brief(row))
            all_defs = await self._gather_metric_defs(
                session, include_key=row.metric_key, include_defn=defn, tenant_id=tenant_id
            )
            measure_keys = await self._gather_measure_keys(session, tenant_id=tenant_id)
            # PRD 5.9 闭环：把"待发布指标"并入 base 层构造副本层，使 ④ SQL dry-run / ⑥ 试跑
            # 能真正编译并试跑该指标（运行时层此刻尚未包含它）；构造失败则回落 base 层（降级 warning）。
            merged_layer = build_layer_with_metric(layer, self._metric_brief(row))
            result = await validate_metric_definition(
                defn,
                metric_key=row.metric_key,
                all_metric_defs=all_defs,
                measure_keys=measure_keys,
                layer=merged_layer or layer,
                engine=engine,
                deep=deep,
            )
            if not result.passed:
                return {"ok": False, "validation": result.to_dict()}
            before = defn
            row.status = PUBLISHED
            row.version = row.version + 1
            row.updated_by = operator
            row.approved_by = approved_by
            # 落库 yaml_defn（PRD 5.9：发布指标生成 manifest 兼容口径 dict，供 KB 重建与可观测）
            row.yaml_defn = metric_to_yaml_defn(self._metric_brief(row))
            after = metric_core_defn(self._metric_brief(row))
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=row.version, change_type="publish",
                    before_json=before, after_json=after,
                    operator=operator, approved_by=approved_by,
                )
            )
            await session.commit()
            await session.refresh(row)
            return {"ok": True, "metric": self._metric_brief(row), "version": row.version}

    async def deprecate_metric(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None:
                return None
            if not can_transition(row.status, DEPRECATED):
                raise ValueError(f"非法状态转移：{row.status} -> {DEPRECATED}")
            row.status = DEPRECATED
            row.version = row.version + 1
            row.updated_by = operator
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=row.version, change_type="deprecate",
                    after_json=metric_core_defn(self._metric_brief(row)),
                    operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._metric_brief(row)

    async def rollback_metric(
        self,
        metric_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None:
                return None
            # 同一 revision 可能有多条审计（create v1 / submit v1 —— submit 不涨版本号）；
            # 回滚目标是「该版本最终的口径」，取该 version 下最新一条快照。
            vid = (
                await session.execute(
                    select(models.SemanticMetricVersionModel)
                    .where(
                        models.SemanticMetricVersionModel.metric_id == row.id,
                        models.SemanticMetricVersionModel.version == version,
                    )
                    .order_by(models.SemanticMetricVersionModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vid is None:
                return None
            before = metric_core_defn(self._metric_brief(row))
            restored = vid.after_json or {}
            row.name = restored.get("name") or row.name
            row.metric_type = restored.get("metric_type") or row.metric_type
            row.type_params = restored.get("type_params") or row.type_params
            row.aliases = restored.get("aliases") or row.aliases
            row.unit = restored.get("unit") or ""
            row.description = restored.get("description") or ""
            row.scale = float(restored.get("scale") or row.scale or 1.0)
            row.round = int(restored.get("round") or row.round or 4)
            row.granularities = restored.get("granularities") or row.granularities
            row.pop_types = restored.get("pop_types") or row.pop_types
            row.owner = restored.get("owner") or row.owner
            row.status = PUBLISHED
            row.version = row.version + 1
            row.updated_by = operator
            row.approved_by = approved_by
            after = metric_core_defn(self._metric_brief(row))
            session.add(
                models.SemanticMetricVersionModel(
                    metric_id=row.id, version=row.version, change_type="rollback",
                    before_json=before, after_json=after,
                    operator=operator, approved_by=approved_by,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._metric_brief(row)

    async def list_metric_versions(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None and tenant_id:
                row = await self._metric_row(session, metric_key, "")  # 回落全局（只读）
            if row is None:
                return []
            mid = row.id
            rows = (
                await session.execute(
                    select(models.SemanticMetricVersionModel)
                    .where(models.SemanticMetricVersionModel.metric_id == mid)
                    .order_by(models.SemanticMetricVersionModel.version)
                )
            ).scalars().all()
            return [
                {
                    "version": v.version,
                    "change_type": v.change_type,
                    "operator": v.operator,
                    "approved_by": v.approved_by or "",
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "after_json": v.after_json,
                }
                for v in rows
            ]

    async def get_metric_version(
        self, metric_key: str, version: int, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None and tenant_id:
                row = await self._metric_row(session, metric_key, "")  # 回落全局（只读）
            if row is None:
                return None
            mid = row.id
            v = (
                await session.execute(
                    select(models.SemanticMetricVersionModel)
                    .where(
                        models.SemanticMetricVersionModel.metric_id == mid,
                        models.SemanticMetricVersionModel.version == version,
                    )
                    .order_by(models.SemanticMetricVersionModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if v is None:
                return None
            return {
                "version": v.version,
                "change_type": v.change_type,
                "operator": v.operator,
                "approved_by": v.approved_by or "",
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "after_json": v.after_json,
            }

    async def list_all_metric_defs(
        self, *, tenant_id: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """引用校验的**取值域**：全局 ∪ 本租户（可见性，不是归属）。

        刻意用可见性而非归属：租户新指标引用一个**全局** measure 是合法的，若按归属收窄，
        校验会把它判成"引用不存在"。排序上全局在前、租户在后，让同名条目由租户覆盖 ——
        这正是 overlay 的语义（租户条目优先）。
        """
        async with self._sf() as session:
            stmt = select(models.SemanticMetricModel)
            clause = self._visible_tenant_clause(
                models.SemanticMetricModel.tenant_id, tenant_id
            )
            if clause is not None:
                stmt = stmt.where(clause)
            stmt = stmt.order_by(models.SemanticMetricModel.tenant_id, models.SemanticMetricModel.id)
            rows = (await session.execute(stmt)).scalars().all()
            return {
                r.metric_key: {
                    "metric_type": r.metric_type,
                    "type_params": dict(r.type_params or {}),
                    "status": r.status,
                    "tenant_id": getattr(r, "tenant_id", "") or "",
                }
                for r in rows
            }

    async def list_all_measure_keys(
        self, *, tenant_id: Optional[str] = None
    ) -> Set[str]:
        """同上：度量的引用取值域 = 全局 ∪ 本租户（经宿主模型收敛）。"""
        async with self._sf() as session:
            stmt = select(models.SemanticMeasureModel.measure_key)
            clause = self._visible_measure_clause(tenant_id)
            if clause is not None:
                stmt = stmt.where(clause)
            rows = (await session.execute(stmt)).scalars().all()
            return set(rows)

    async def get_metric_def(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._metric_row(session, metric_key, tenant_id)
            if row is None and tenant_id:
                row = await self._metric_row(session, metric_key, "")  # 回落全局（只读）
            if row is None:
                return None
            return metric_core_defn(self._metric_brief(row))

    # ---------- 写路径：维度（P2，与指标对称）----------

    async def _dimension_brief(self, session, row) -> Dict[str, Any]:
        model_key = await self._dimension_model_key(session, getattr(row, "model_id", None))
        return {
            "id": row.id,
            "dimension_key": row.dimension_key,
            "name": row.name,
            "dim_type": row.dim_type,
            "expr": row.expr,
            "time_granularity": row.time_granularity,
            "description": row.description or "",
            "status": row.status,
            "version": row.version,
            "model_key": model_key,
            "model_id": row.model_id,
            "approved_by": row.approved_by or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def _dimension_model_key(self, session, model_id: Optional[int]) -> str:
        if not model_id:
            return ""
        return (
            await session.execute(
                select(models.SemanticModelModel.model_key).where(
                    models.SemanticModelModel.id == model_id
                )
            )
        ).scalar_one_or_none() or ""

    async def _dimension_core(self, session, row) -> Dict[str, Any]:
        """只取口径核心字段（不含 created_at/updated_at）。

        写方法在改完 status/version 后用它落版本快照：核心字段无 server_default/onupdate，
        flush 后仍保留在内存，读取不触发隐式 IO；而时间戳列会被 onupdate 过期，
        此时连同 model_key 子查询（会触发 autoflush）一起读会抛 MissingGreenlet。
        """
        return dimension_core_defn(
            {
                "dimension_key": row.dimension_key,
                "name": row.name,
                "dim_type": row.dim_type,
                "expr": row.expr,
                "time_granularity": row.time_granularity,
                "description": row.description or "",
                "model_key": await self._dimension_model_key(
                    session, getattr(row, "model_id", None)
                ),
            }
        )

    def _dimension_brief_from(self, defn, *, id, model_id, dimension_key, model_key, status, version, created_at=None):
        return {
            "id": id,
            "dimension_key": dimension_key,
            "name": defn.get("name") or dimension_key,
            "dim_type": defn.get("dim_type") or "categorical",
            "expr": defn.get("expr") or "",
            "time_granularity": defn.get("time_granularity") or "day",
            "description": defn.get("description") or "",
            "status": status,
            "version": version,
            "model_key": model_key,
            "model_id": model_id,
            "created_at": created_at,
            "updated_at": created_at,
        }

    async def create_dimension_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """起草维度。维度的租户归属**继承宿主模型**，所以这里没有 tenant_id 入参可选：
        宿主模型只能落在调用方可见的（全局 + 本租户）模型上；``tenant_id`` 非 None 时
        进一步要求宿主模型**恰好**属于该租户 —— 否则租户就能往全局模型上挂自己的维度。"""
        async with self._sf() as session:
            model_key = defn.get("model_key") or ""
            stmt = select(models.SemanticModelModel).where(
                models.SemanticModelModel.model_key == model_key
            )
            clause = (
                self._owner_tenant_clause(
                    models.SemanticModelModel.tenant_id, tenant_id
                )
                if tenant_id is not None
                else None
            )
            if clause is not None:
                stmt = stmt.where(clause)
            model = (await session.execute(stmt)).scalar_one_or_none()
            if model is None:
                raise ValueError(
                    f"维度所属语义模型不存在或不属于当前租户：{model_key}"
                )
            key = defn.get("dimension_key") or derive_dimension_key(defn)
            base, n = key, 2
            while True:
                exists = (
                    await session.execute(
                        select(func.count())
                        .select_from(models.SemanticDimensionModel)
                        .where(
                            models.SemanticDimensionModel.model_id == model.id,
                            models.SemanticDimensionModel.dimension_key == key,
                        )
                    )
                ).scalar() or 0
                if not exists:
                    break
                key = f"{base}_{n}"
                n += 1
            row = models.SemanticDimensionModel(
                model_id=model.id,
                dimension_key=key,
                name=defn.get("name") or key,
                dim_type=defn.get("dim_type") or "categorical",
                expr=defn.get("expr") or "",
                time_granularity=defn.get("time_granularity") or "day",
                description=defn.get("description") or "",
                status=DRAFT,
                version=1,
            )
            session.add(row)
            await session.flush()
            core = dimension_core_defn(
                self._dimension_brief_from(
                    defn, id=row.id, model_id=model.id, dimension_key=key,
                    model_key=model_key, status=DRAFT, version=1,
                )
            )
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=1, change_type="create",
                    after_json=core, operator=operator,
                )
            )
            await session.commit()
            return self._dimension_brief_from(
                defn, id=row.id, model_id=model.id, dimension_key=key,
                model_key=model_key, status=DRAFT, version=1,
                created_at=datetime.now().isoformat(),
            )

    async def update_dimension(
        self,
        dimension_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None:
                return None
            if row.status not in (DRAFT, REVIEW):
                raise ValueError(
                    f"只能编辑草稿/待审维度（当前 {row.status}）；"
                    f"已发布/已废弃请先废弃或基于新版本重起草"
                )
            before = await self._dimension_core(session, row)
            row.name = defn.get("name") or row.name
            row.dim_type = defn.get("dim_type") or row.dim_type
            row.expr = defn.get("expr") or row.expr
            row.time_granularity = defn.get("time_granularity") or row.time_granularity
            row.description = defn.get("description") or ""
            after = await self._dimension_core(session, row)
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=row.version, change_type="update",
                    before_json=before, after_json=after, operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return await self._dimension_brief(session, row)

    async def submit_dimension_review(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None:
                return None
            if not can_transition(row.status, REVIEW):
                raise ValueError(f"非法状态转移：{row.status} -> {REVIEW}")
            row.status = REVIEW
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=row.version, change_type="submit",
                    after_json=await self._dimension_core(session, row),
                    operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return await self._dimension_brief(session, row)

    async def publish_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None:
                return {"ok": False, "error": "not_found"}
            if not can_transition(row.status, PUBLISHED):
                return {"ok": False, "error": "illegal_transition", "current": row.status}
            defn = await self._dimension_core(session, row)
            result = validate_dimension_definition(defn)
            if not result.passed:
                return {"ok": False, "validation": result.to_dict()}
            row.status = PUBLISHED
            row.version = row.version + 1
            row.approved_by = approved_by
            after = await self._dimension_core(session, row)
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=row.version, change_type="publish",
                    after_json=after, operator=operator, approved_by=approved_by,
                )
            )
            await session.commit()
            await session.refresh(row)
            return {"ok": True, "dimension": await self._dimension_brief(session, row), "version": row.version}

    async def deprecate_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None:
                return None
            if not can_transition(row.status, DEPRECATED):
                raise ValueError(f"非法状态转移：{row.status} -> {DEPRECATED}")
            row.status = DEPRECATED
            row.version = row.version + 1
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=row.version, change_type="deprecate",
                    after_json=await self._dimension_core(session, row),
                    operator=operator,
                )
            )
            await session.commit()
            await session.refresh(row)
            return await self._dimension_brief(session, row)

    async def rollback_dimension(
        self,
        dimension_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None:
                return None
            # 同 metric：同一 revision 可能有多条审计（create v1 / submit v1），
            # 取该 version 下最新一条快照作为回滚目标。
            vid = (
                await session.execute(
                    select(models.SemanticDimensionVersionModel)
                    .where(
                        models.SemanticDimensionVersionModel.dimension_id == row.id,
                        models.SemanticDimensionVersionModel.version == version,
                    )
                    .order_by(models.SemanticDimensionVersionModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vid is None:
                return None
            before = await self._dimension_core(session, row)
            restored = vid.after_json or {}
            row.name = restored.get("name") or row.name
            row.dim_type = restored.get("dim_type") or row.dim_type
            row.expr = restored.get("expr") or row.expr
            row.time_granularity = restored.get("time_granularity") or row.time_granularity
            row.description = restored.get("description") or ""
            row.status = PUBLISHED
            row.version = row.version + 1
            row.approved_by = approved_by
            after = await self._dimension_core(session, row)
            session.add(
                models.SemanticDimensionVersionModel(
                    dimension_id=row.id, version=row.version, change_type="rollback",
                    before_json=before, after_json=after,
                    operator=operator, approved_by=approved_by,
                )
            )
            await session.commit()
            await session.refresh(row)
            return await self._dimension_brief(session, row)

    async def list_dimension_versions(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None and tenant_id:
                row = await self._dimension_row(session, dimension_key, "")  # 回落全局
            if row is None:
                return []
            did = row.id
            rows = (
                await session.execute(
                    select(models.SemanticDimensionVersionModel)
                    .where(models.SemanticDimensionVersionModel.dimension_id == did)
                    .order_by(models.SemanticDimensionVersionModel.version)
                )
            ).scalars().all()
            return [
                {
                    "version": v.version,
                    "change_type": v.change_type,
                    "operator": v.operator,
                    "approved_by": v.approved_by or "",
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "after_json": v.after_json,
                }
                for v in rows
            ]

    async def get_dimension_def(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        async with self._sf() as session:
            row = await self._dimension_row(session, dimension_key, tenant_id)
            if row is None and tenant_id:
                row = await self._dimension_row(session, dimension_key, "")  # 回落全局
            if row is None:
                return None
            return await self._dimension_core(session, row)


class SqlAuditLogRepository:
    """合规审计日志 MySQL 实现（PRD FR13）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, event: Dict[str, Any]) -> int:
        async with self._sf() as session:
            row = models.AuditLogModel(
                event_type=event.get("event_type") or "unknown",
                level=event.get("level") or "info",
                session_id=event.get("session_id") or "",
                user_id=event.get("user_id") or "",
                tenant_id=event.get("tenant_id") or "",
                round_id=event.get("round_id") or "",
                summary=event.get("summary") or "",
                detail_json=event.get("detail_json") or event.get("details") or {},
                client_info=event.get("client_info") or "",
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def query(
        self,
        *,
        event_type: Optional[str] = None,
        level: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            stmt = select(models.AuditLogModel)
            if event_type:
                stmt = stmt.where(models.AuditLogModel.event_type == event_type)
            if level:
                stmt = stmt.where(models.AuditLogModel.level == level)
            if session_id:
                stmt = stmt.where(models.AuditLogModel.session_id == session_id)
            if user_id:
                stmt = stmt.where(models.AuditLogModel.user_id == user_id)
            total = (
                await session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar() or 0
            rows = (
                await session.execute(
                    stmt.order_by(models.AuditLogModel.id.desc()).limit(limit).offset(offset)
                )
            ).scalars().all()
            items = [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "event_type": r.event_type,
                    "level": r.level,
                    "session_id": r.session_id,
                    "user_id": r.user_id,
                    "tenant_id": r.tenant_id,
                    "round_id": r.round_id,
                    "summary": r.summary,
                    "detail_json": r.detail_json,
                }
                for r in rows
            ]
            return items, int(total)

    async def stats(self) -> Dict[str, Any]:
        async with self._sf() as session:
            by_type = dict(
                (
                    await session.execute(
                        select(models.AuditLogModel.event_type, func.count())
                        .group_by(models.AuditLogModel.event_type)
                    )
                ).all()
            )
            by_level = dict(
                (
                    await session.execute(
                        select(models.AuditLogModel.level, func.count())
                        .group_by(models.AuditLogModel.level)
                    )
                ).all()
            )
            return {"by_type": by_type, "by_level": by_level}


class SqlErrorSampleRepository:
    """错误样本池 MySQL 实现（PRD FR6）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, sample: Dict[str, Any]) -> int:
        async with self._sf() as session:
            row = models.ErrorSampleModel(
                session_id=sample.get("session_id") or "",
                user_id=sample.get("user_id") or "",
                tenant_id=sample.get("tenant_id") or "",
                round_id=sample.get("round_id") or "",
                error_code=sample.get("error_code") or "",
                category=int(sample.get("category") or 0),
                category_label=sample.get("category_label") or "",
                node=sample.get("node") or "",
                message=sample.get("message") or "",
                detail=sample.get("detail") or "",
                retryable=bool(sample.get("retryable", False)),
                recovered=bool(sample.get("recovered", False)),
                fix_hint=sample.get("fix_hint") or "",
                attempts=int(sample.get("attempts") or 1),
                trace_snippet=sample.get("trace_snippet") or "",
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def query(
        self,
        *,
        category: Optional[int] = None,
        node: Optional[str] = None,
        recovered: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        async with self._sf() as session:
            stmt = select(models.ErrorSampleModel)
            if category is not None:
                stmt = stmt.where(models.ErrorSampleModel.category == category)
            if node:
                stmt = stmt.where(models.ErrorSampleModel.node == node)
            if recovered is not None:
                stmt = stmt.where(models.ErrorSampleModel.recovered == recovered)
            total = (
                await session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar() or 0
            rows = (
                await session.execute(
                    stmt.order_by(models.ErrorSampleModel.id.desc()).limit(limit).offset(offset)
                )
            ).scalars().all()
            items = [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "session_id": r.session_id,
                    "user_id": r.user_id,
                    "tenant_id": r.tenant_id,
                    "round_id": r.round_id,
                    "error_code": r.error_code,
                    "category": r.category,
                    "category_label": r.category_label,
                    "node": r.node,
                    "message": r.message,
                    "detail": r.detail,
                    "retryable": r.retryable,
                    "recovered": r.recovered,
                    "fix_hint": r.fix_hint,
                    "attempts": r.attempts,
                }
                for r in rows
            ]
            return items, int(total)

    async def stats(self) -> Dict[str, Any]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        models.ErrorSampleModel.category,
                        models.ErrorSampleModel.category_label,
                        func.count(),
                    ).group_by(
                        models.ErrorSampleModel.category,
                        models.ErrorSampleModel.category_label,
                    )
                )
            ).all()
            by_category_label: Dict[str, int] = {}
            for (_cat, label, cnt) in rows:
                by_category_label[label or "未知"] = by_category_label.get(label or "未知", 0) + cnt
            total = sum(by_category_label.values())
            recovered_rows = (
                await session.execute(
                    select(func.count()).where(models.ErrorSampleModel.recovered.is_(True))
                )
            ).scalar() or 0
            return {
                "total": total,
                "recovered": int(recovered_rows),
                "recovery_rate": round(recovered_rows / total, 4) if total else 0.0,
                "by_category": by_category_label,
            }


class SqlRoleRepository:
    """角色仓储（MySQL）；name 为主键（内置三角色 + 自定义角色）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_data(r: models.RoleModel) -> RoleData:
        return RoleData(
            name=r.name,
            label=r.label or "",
            description=r.description or "",
            permissions=list(r.permissions or []),
            status=r.status or "active",
            is_builtin=bool(r.is_builtin),
            created_at=r.created_at,
        )

    async def add(self, role: RoleData) -> RoleData:
        async with self._sf() as session:
            obj = models.RoleModel(
                name=role.name,
                label=role.label,
                description=role.description,
                permissions=list(role.permissions),
                status=role.status or "active",
                is_builtin=role.is_builtin,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return self._to_data(obj)

    async def get(self, name: str) -> Optional[RoleData]:
        async with self._sf() as session:
            obj = await session.get(models.RoleModel, name)
            return self._to_data(obj) if obj else None

    async def list_all(self) -> List[RoleData]:
        async with self._sf() as session:
            rows = (await session.execute(
                select(models.RoleModel).order_by(models.RoleModel.created_at.asc())
            )).scalars().all()
            return [self._to_data(r) for r in rows]

    async def update(
        self,
        name: str,
        *,
        label: Optional[str] = None,
        description: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        status: Optional[str] = None,
    ) -> Optional[RoleData]:
        values: Dict[str, Any] = {}
        if label is not None:
            values["label"] = label
        if description is not None:
            values["description"] = description
        if permissions is not None:
            values["permissions"] = list(permissions)
        if status is not None:
            values["status"] = status
        if values:
            async with self._sf() as session:
                await session.execute(
                    update(models.RoleModel).where(models.RoleModel.name == name).values(**values)
                )
                await session.commit()
        return await self.get(name)

    async def delete(self, name: str) -> bool:
        async with self._sf() as session:
            obj = await session.get(models.RoleModel, name)
            if obj is None:
                return False
            await session.delete(obj)
            await session.commit()
            return True

    async def count(self) -> int:
        async with self._sf() as session:
            return int((await session.execute(select(func.count()).select_from(models.RoleModel))).scalar() or 0)


class SqlMetricGrantRepository:
    """指标授权 MySQL 实现（FR13 enforce 指标白名单）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, grant: MetricGrantData) -> MetricGrantData:
        async with self._sf() as session:
            obj = models.MetricGrantModel(
                tenant_id=grant.tenant_id,
                user_id=grant.user_id,
                role=grant.role,
                metric_key=grant.metric_key,
                granted_by=grant.granted_by,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return MetricGrantData.from_orm_row(obj)

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[MetricGrantData]:
        async with self._sf() as session:
            stmt = (
                select(models.MetricGrantModel)
                .where(
                    or_(
                        models.MetricGrantModel.tenant_id == tenant_id,
                        models.MetricGrantModel.tenant_id == "",
                    ),
                    or_(
                        and_(models.MetricGrantModel.user_id == user_id, user_id != ""),
                        and_(models.MetricGrantModel.role == role, role != ""),
                        and_(models.MetricGrantModel.user_id == "", models.MetricGrantModel.role == ""),
                    ),
                )
                .order_by(models.MetricGrantModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [MetricGrantData.from_orm_row(r) for r in rows]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[MetricGrantData]:
        async with self._sf() as session:
            stmt = select(models.MetricGrantModel)
            if tenant_id:
                stmt = stmt.where(models.MetricGrantModel.tenant_id == tenant_id)
            stmt = stmt.order_by(models.MetricGrantModel.id).limit(max(limit, 1))
            rows = (await session.execute(stmt)).scalars().all()
            return [MetricGrantData.from_orm_row(r) for r in rows]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, metric_key: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(models.MetricGrantModel).where(
                    models.MetricGrantModel.tenant_id == tenant_id,
                    models.MetricGrantModel.user_id == user_id,
                    models.MetricGrantModel.role == role,
                    models.MetricGrantModel.metric_key == metric_key,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (await session.execute(select(func.count()).select_from(models.MetricGrantModel))).scalar() or 0
            )


class SqlDataScopeRepository:
    """行权限 MySQL 实现（FR13 enforce 行级过滤下推）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, scope: DataScopeData) -> DataScopeData:
        async with self._sf() as session:
            obj = models.DataScopeModel(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                role=scope.role,
                dimension=scope.dimension,
                allowed_values=list(scope.allowed_values or []),
                granted_by=scope.granted_by,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return DataScopeData.from_orm_row(obj)

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[DataScopeData]:
        async with self._sf() as session:
            stmt = (
                select(models.DataScopeModel)
                .where(
                    or_(
                        models.DataScopeModel.tenant_id == tenant_id,
                        models.DataScopeModel.tenant_id == "",
                    ),
                    or_(
                        and_(models.DataScopeModel.user_id == user_id, user_id != ""),
                        and_(models.DataScopeModel.role == role, role != ""),
                        and_(models.DataScopeModel.user_id == "", models.DataScopeModel.role == ""),
                    ),
                )
                .order_by(models.DataScopeModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [DataScopeData.from_orm_row(r) for r in rows]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[DataScopeData]:
        async with self._sf() as session:
            stmt = select(models.DataScopeModel)
            if tenant_id:
                stmt = stmt.where(models.DataScopeModel.tenant_id == tenant_id)
            stmt = stmt.order_by(models.DataScopeModel.id).limit(max(limit, 1))
            rows = (await session.execute(stmt)).scalars().all()
            return [DataScopeData.from_orm_row(r) for r in rows]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, dimension: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(models.DataScopeModel).where(
                    models.DataScopeModel.tenant_id == tenant_id,
                    models.DataScopeModel.user_id == user_id,
                    models.DataScopeModel.role == role,
                    models.DataScopeModel.dimension == dimension,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (await session.execute(select(func.count()).select_from(models.DataScopeModel))).scalar() or 0
            )


class SqlDimensionPolicyRepository:
    """列/维度 deny 策略 MySQL 实现（FR13 enforce 列级硬剔除）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, policy: DimensionPolicyData) -> DimensionPolicyData:
        async with self._sf() as session:
            obj = models.DimensionPolicyModel(
                tenant_id=policy.tenant_id,
                user_id=policy.user_id,
                role=policy.role,
                dimension=policy.dimension,
                denied=policy.denied,
                granted_by=policy.granted_by,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return DimensionPolicyData.from_orm_row(obj)

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[DimensionPolicyData]:
        async with self._sf() as session:
            stmt = (
                select(models.DimensionPolicyModel)
                .where(
                    or_(
                        models.DimensionPolicyModel.tenant_id == tenant_id,
                        models.DimensionPolicyModel.tenant_id == "",
                    ),
                    or_(
                        and_(models.DimensionPolicyModel.user_id == user_id, user_id != ""),
                        and_(models.DimensionPolicyModel.role == role, role != ""),
                        and_(models.DimensionPolicyModel.user_id == "", models.DimensionPolicyModel.role == ""),
                    ),
                )
                .order_by(models.DimensionPolicyModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [DimensionPolicyData.from_orm_row(r) for r in rows]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[DimensionPolicyData]:
        async with self._sf() as session:
            stmt = select(models.DimensionPolicyModel)
            if tenant_id:
                stmt = stmt.where(models.DimensionPolicyModel.tenant_id == tenant_id)
            stmt = stmt.order_by(models.DimensionPolicyModel.id).limit(max(limit, 1))
            rows = (await session.execute(stmt)).scalars().all()
            return [DimensionPolicyData.from_orm_row(r) for r in rows]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, dimension: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(models.DimensionPolicyModel).where(
                    models.DimensionPolicyModel.tenant_id == tenant_id,
                    models.DimensionPolicyModel.user_id == user_id,
                    models.DimensionPolicyModel.role == role,
                    models.DimensionPolicyModel.dimension == dimension,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (await session.execute(select(func.count()).select_from(models.DimensionPolicyModel))).scalar() or 0
            )


class SqlSemanticPublishedRepository:
    """语义层发布快照 MySQL 实现（混合方案）。

    - get_latest_published：先取该租户 published 的最新版本；无则回落全局(tenant_id='')。
    - 排序 `tenant_id DESC, version DESC, id DESC`：非空 tenant_id 排在 "" 之前，
      故同一次查询即可实现「租户专属优先、其次版本最新」。
    - mode 区分 full（整份快照）/ overlay（差异条目）；链式叠加在语义层完成。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, rec: SemanticPublishedData) -> SemanticPublishedData:
        async with self._sf() as session:
            obj = models.SemanticPublishedModel(
                tenant_id=rec.tenant_id,
                version=rec.version,
                state=rec.state,
                mode=rec.mode or "full",
                payload=dict(rec.payload or {}),
                created_by=rec.created_by,
                published_at=func.now() if rec.state == "published" else None,
                approved_by=rec.approved_by or "",
                rejected_by=rec.rejected_by or "",
                reject_reason=rec.reject_reason or "",
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return SemanticPublishedData.from_orm_row(obj)

    async def get(self, snapshot_id: int) -> Optional[SemanticPublishedData]:
        async with self._sf() as session:
            row = await session.get(models.SemanticPublishedModel, snapshot_id)
            return SemanticPublishedData.from_orm_row(row) if row is not None else None

    async def set_state(
        self,
        *,
        snapshot_id: int,
        state: str,
        operator: str = "",
        approved_by: str = "",
        rejected_by: str = "",
        reject_reason: str = "",
    ) -> Optional[SemanticPublishedData]:
        async with self._sf() as session:
            row = await session.get(models.SemanticPublishedModel, snapshot_id)
            if row is None:
                return None
            row.state = state
            if state == "published":
                if row.published_at is None:
                    row.published_at = func.now()
                if approved_by:
                    row.approved_by = approved_by
                elif operator:
                    row.approved_by = operator
            elif state == "deprecated":
                # 废弃沿用审批人信息；不清除
                pass
            elif state == "draft" and (rejected_by or reject_reason):
                # 驳回到草稿：记录驳回人 / 原因（便于审计与重新提交）
                if rejected_by:
                    row.rejected_by = rejected_by
                if reject_reason:
                    row.reject_reason = reject_reason
            await session.commit()
            # 铁律：commit 后必须 refresh 再出参，否则读 server 端列会 MissingGreenlet
            await session.refresh(row)
            return SemanticPublishedData.from_orm_row(row)

    async def get_latest_published(self, *, tenant_id: str) -> Optional[SemanticPublishedData]:
        async with self._sf() as session:
            stmt = select(models.SemanticPublishedModel).where(
                models.SemanticPublishedModel.state == "published"
            )
            if tenant_id:
                stmt = stmt.where(
                    or_(
                        models.SemanticPublishedModel.tenant_id == tenant_id,
                        models.SemanticPublishedModel.tenant_id == "",
                    )
                )
            else:
                stmt = stmt.where(models.SemanticPublishedModel.tenant_id == "")
            stmt = stmt.order_by(
                models.SemanticPublishedModel.tenant_id.desc(),
                models.SemanticPublishedModel.version.desc(),
                models.SemanticPublishedModel.id.desc(),
            ).limit(1)
            row = (await session.execute(stmt)).scalars().first()
            return SemanticPublishedData.from_orm_row(row) if row is not None else None

    async def list_for(
        self, *, tenant_id: Optional[str] = None, limit: int = 100, visible_scope: bool = False
    ) -> List[SemanticPublishedData]:
        async with self._sf() as session:
            stmt = (
                select(models.SemanticPublishedModel)
                .order_by(models.SemanticPublishedModel.version.desc(), models.SemanticPublishedModel.id.desc())
                .limit(max(limit, 1))
            )
            if visible_scope:
                # 读作用域：None=全量 / 空串=仅全局 / 具体 id=全局+该租户
                clause = SqlSemanticRegistryRepository._visible_tenant_clause(
                    models.SemanticPublishedModel.tenant_id, tenant_id
                )
                if clause is not None:
                    stmt = stmt.where(clause)
            else:
                stmt = stmt.where(models.SemanticPublishedModel.tenant_id == (tenant_id or ""))
            rows = (await session.execute(stmt)).scalars().all()
            return [SemanticPublishedData.from_orm_row(r) for r in rows]

    async def count_for(self, *, tenant_id: str) -> int:
        async with self._sf() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(models.SemanticPublishedModel)
                        .where(models.SemanticPublishedModel.tenant_id == (tenant_id or ""))
                    )
                ).scalar()
                or 0
            )

    async def delete(self, *, tenant_id: str, version: int) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(models.SemanticPublishedModel).where(
                    models.SemanticPublishedModel.tenant_id == (tenant_id or ""),
                    models.SemanticPublishedModel.version == version,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def count(self) -> int:
        async with self._sf() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(models.SemanticPublishedModel)
                    )
                ).scalar()
                or 0
            )
