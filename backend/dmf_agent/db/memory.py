"""内存实现（降级 / 测试用）。

MySQL 未启用或连接失败时，由 db.factory 装配这些实现。
指标元数据内存版内置少量示例指标，供无外部依赖时联调走通全链路。
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

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
from dmf_agent.schemas.knowledge import (
    ChunkData,
    DocumentData,
    KnowledgeBaseData,
)
from dmf_agent.schemas.memory import DomainMemoryData, MemorySummaryData, UserPreferenceData
from dmf_agent.schemas.metrics import MetricData
from dmf_agent.schemas.feedback import BadCaseData, FewShotData
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


def _now() -> datetime:
    return datetime.now()


def _new_id() -> str:
    return uuid4().hex


# 示例指标元数据（真实数据后续由老系统 metadata/hm_meta 迁移导入）
SAMPLE_METRICS: List[MetricData] = [
    MetricData(code="gmv", name="GMV", description="成交总额", unit="元", category="交易",
               dimensions=["渠道", "地区", "品类"]),
    MetricData(code="dau", name="日活跃用户数", description="DAU", unit="人", category="用户",
               dimensions=["渠道", "平台"]),
    MetricData(code="new_users", name="新增用户数", description="每日新注册用户", unit="人", category="用户",
               dimensions=["渠道", "地区"]),
    MetricData(code="pay_rate", name="付费转化率", description="付费用户/活跃用户", unit="%", category="交易",
               dimensions=["渠道"]),
    MetricData(code="arpu", name="客单价", description="GMV/订单数", unit="元", category="交易",
               dimensions=["品类"]),
]


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: Dict[str, SessionData] = {}

    async def create(
        self, user_id: str, title: Optional[str] = None, tenant_id: str = ""
    ) -> SessionData:
        async with self._lock:
            session = SessionData(
                id=_new_id(), user_id=user_id, tenant_id=tenant_id, title=title or "新会话",
                created_at=_now(), updated_at=_now(),
            )
            self._sessions[session.id] = session
            return session

    async def get(self, session_id: str) -> Optional[SessionData]:
        return self._sessions.get(session_id)

    async def list_by_user(self, user_id: str) -> List[SessionData]:
        items = [s for s in self._sessions.values() if s.user_id == user_id]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items

    async def list_by_tenant(self, tenant_id: str) -> List[SessionData]:
        items = [s for s in self._sessions.values() if s.tenant_id == tenant_id]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items

    async def list_all(self, limit: int = 500) -> List[SessionData]:
        """全部会话（管理面用：记忆管理页补 id→title 映射）。"""
        items = sorted(
            self._sessions.values(),
            key=lambda s: s.updated_at or s.created_at or _now(),
            reverse=True,
        )
        return items[: max(limit, 1)]

    async def rename(self, session_id: str, title: str) -> Optional[SessionData]:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title
        session.updated_at = _now()
        return session

    async def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


class InMemoryMessageRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._messages: List[MessageData] = []

    async def add(self, message: MessageData) -> MessageData:
        async with self._lock:
            if not message.id:
                message.id = _new_id()
            if message.created_at is None:
                message.created_at = _now()
            self._messages.append(message)
            return message

    async def list_by_session(self, session_id: str) -> List[MessageData]:
        items = [m for m in self._messages if m.session_id == session_id]
        items.sort(key=lambda m: (m.created_at or _now(),))
        return items

    async def delete_by_session(self, session_id: str) -> int:
        async with self._lock:
            remain = [m for m in self._messages if m.session_id != session_id]
            removed = len(self._messages) - len(remain)
            self._messages = remain
            return removed

    async def delete_after(
        self, session_id: str, message_id: str, *, inclusive: bool = True
    ) -> int:
        """删除该会话中 `message_id` 及其之后的全部消息（编辑重发的「替换该轮」）。"""
        async with self._lock:
            items = [m for m in self._messages if m.session_id == session_id]
            # 排序口径必须与 list_by_session 完全一致 —— 否则"界面上看到的顺序"与
            # "被裁掉的区间"可能错位（见本类 list_by_session 的实现）。
            items.sort(key=lambda m: (m.created_at or _now(),))
            ids = [m.id for m in items]
            if message_id not in ids:
                return 0
            cut = ids.index(message_id) if inclusive else ids.index(message_id) + 1
            victims = set(ids[cut:])
            if not victims:
                return 0
            before = len(self._messages)
            self._messages = [m for m in self._messages if m.id not in victims]
            return before - len(self._messages)

    async def get(self, message_id: str) -> Optional[MessageData]:
        for m in self._messages:
            if m.id == message_id:
                return m
        return None


class InMemoryDocumentRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._docs: Dict[str, DocumentData] = {}

    async def add(self, doc: DocumentData) -> DocumentData:
        async with self._lock:
            if not doc.id:
                doc.id = _new_id()
            doc.created_at = doc.created_at or _now()
            self._docs[doc.id] = doc
            return doc

    async def get(self, doc_id: str) -> Optional[DocumentData]:
        return self._docs.get(doc_id)

    async def list(
        self,
        keyword: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        authority_level: Optional[str] = None,
        source_uri: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> List[DocumentData]:
        items = list(self._docs.values())
        if keyword:
            items = [d for d in items if keyword in d.title]
        if knowledge_base_id:
            items = [d for d in items if d.knowledge_base_id == knowledge_base_id]
        if authority_level:
            items = [
                d for d in items if (d.authority_level or "user") == authority_level
            ]
        if source_uri:
            items = [d for d in items if d.source_uri == source_uri]
        # 租户过滤：None 表示不过滤（鉴权关闭）；"" 只匹配无租户文档
        if tenant_id is not None:
            items = [d for d in items if d.tenant_id == tenant_id]
        # owner_id 过滤：检索隔离用——只取归属当前用户的个人/私有文档
        if owner_id is not None:
            items = [d for d in items if d.owner_id == owner_id]
        items.sort(key=lambda d: d.created_at, reverse=True)
        return items

    async def count_documents(self, knowledge_base_id: str) -> int:
        return sum(
            1 for d in self._docs.values() if d.knowledge_base_id == knowledge_base_id
        )

    async def count_by_authority(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for d in self._docs.values():
            level = d.authority_level or "user"
            counts[level] = counts.get(level, 0) + 1
        return counts

    async def update_status(
        self, doc_id: str, status: str, chunk_count: Optional[int] = None
    ) -> Optional[DocumentData]:
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        doc.status = status
        if chunk_count is not None:
            doc.chunk_count = chunk_count
        doc.updated_at = _now()
        return doc

    async def delete(self, doc_id: str) -> bool:
        return self._docs.pop(doc_id, None) is not None


class InMemoryKnowledgeBaseRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._bases: Dict[str, KnowledgeBaseData] = {}

    async def add(self, base: KnowledgeBaseData) -> KnowledgeBaseData:
        async with self._lock:
            if not base.id:
                base.id = _new_id()
            base.created_at = base.created_at or _now()
            base.updated_at = base.updated_at or _now()
            self._bases[base.id] = base
            return base

    async def get(self, base_id: str) -> Optional[KnowledgeBaseData]:
        return self._bases.get(base_id)

    async def list(
        self,
        type: Optional[str] = None,
        keyword: Optional[str] = None,
        owner_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[KnowledgeBaseData]:
        items = list(self._bases.values())
        if type:
            items = [b for b in items if b.type == type]
        if keyword:
            items = [b for b in items if keyword in b.name]
        if owner_id:
            items = [b for b in items if b.owner_id == owner_id]
        # 租户过滤：None 表示不过滤（鉴权关闭）；"" 只匹配无租户记录
        if tenant_id is not None:
            items = [b for b in items if b.tenant_id == tenant_id]
        items.sort(key=lambda b: b.created_at, reverse=True)
        return items

    async def delete(self, base_id: str) -> bool:
        return self._bases.pop(base_id, None) is not None


class InMemoryChunkRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._chunks: List[ChunkData] = []

    async def add_many(self, chunks: List[ChunkData]) -> List[ChunkData]:
        async with self._lock:
            for c in chunks:
                if not c.id:
                    c.id = _new_id()
            self._chunks.extend(chunks)
            return chunks

    async def list_by_document(self, doc_id: str) -> List[ChunkData]:
        items = [c for c in self._chunks if c.document_id == doc_id]
        items.sort(key=lambda c: c.chunk_index)
        return items

    async def list_all(self) -> List[ChunkData]:
        return list(self._chunks)

    async def delete_by_document(self, doc_id: str) -> int:
        async with self._lock:
            remain = [c for c in self._chunks if c.document_id != doc_id]
            removed = len(self._chunks) - len(remain)
            self._chunks = remain
            return removed


class InMemoryMemorySummaryRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._summaries: Dict[str, MemorySummaryData] = {}

    async def get(self, session_id: str) -> Optional[MemorySummaryData]:
        return self._summaries.get(session_id)

    async def upsert(self, summary: MemorySummaryData) -> MemorySummaryData:
        async with self._lock:
            summary.updated_at = _now()
            self._summaries[summary.session_id] = summary
            return summary

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._summaries.pop(session_id, None)

    async def list_recent(
        self, limit: int = 100, keyword: str = ""
    ) -> List[MemorySummaryData]:
        kw = (keyword or "").strip().lower()
        items = [
            s for s in self._summaries.values()
            if not kw or kw in (s.summary or "").lower()
        ]
        items.sort(key=lambda s: s.updated_at or _now(), reverse=True)
        return items[: max(limit, 1)]

    async def count(self) -> int:
        return len(self._summaries)


class InMemoryConversationStateRepository:
    """结构化对话状态内存仓储（P0-6 多轮对话状态管理）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[str, ConversationStateData] = {}

    async def get(self, session_id: str) -> Optional[ConversationStateData]:
        return self._states.get(session_id)

    async def upsert(self, state: ConversationStateData) -> ConversationStateData:
        async with self._lock:
            state.updated_at = _now()
            self._states[state.session_id] = state
            return state

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            return self._states.pop(session_id, None) is not None

    async def list_recent(
        self, limit: int = 100, tenant_id: Optional[str] = None
    ) -> List[ConversationStateData]:
        items = [
            s for s in self._states.values()
            if tenant_id is None or s.tenant_id == tenant_id
        ]
        items.sort(key=lambda s: s.updated_at or _now(), reverse=True)
        return items[: max(limit, 1)]


class InMemoryMetricRepository:
    """内置示例指标；提供与 SQL 版一致的检索接口。"""

    def __init__(self, metrics: Optional[List[MetricData]] = None) -> None:
        self._lock = asyncio.Lock()
        self._metrics: List[MetricData] = metrics if metrics is not None else list(SAMPLE_METRICS)

    def _match(self, metric: MetricData, name_or_code: str) -> bool:
        text = name_or_code.strip()
        return (
            metric.code.lower() == text.lower()
            or metric.name == text
            or text in metric.name
        )

    async def find_by_name_or_code(self, name_or_code: str) -> Optional[MetricData]:
        for m in self._metrics:
            if self._match(m, name_or_code):
                return m
        return None

    async def search(self, keyword: Optional[str] = None, limit: int = 20) -> List[MetricData]:
        if not keyword:
            return self._metrics[:limit]
        return [m for m in self._metrics if self._match(m, keyword)][:limit]

    async def list_all(self) -> List[MetricData]:
        return list(self._metrics)

    async def upsert_many(self, metrics: List[MetricData]) -> int:
        """按 code 主键批量 upsert（存在则替换，不存在则追加）。"""
        async with self._lock:
            index = {m.code: i for i, m in enumerate(self._metrics)}
            for m in metrics:
                if m.code in index:
                    self._metrics[index[m.code]] = m
                else:
                    index[m.code] = len(self._metrics)
                    self._metrics.append(m)
            return len(metrics)


class InMemorySlangRepository:
    """黑话词典内存实现（与 MySQL 版同接口，测试 / 降级用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: List[SlangEntryData] = []
        self._versions: List[SlangVersionData] = []
        self._rewrite_events: List[Dict[str, Any]] = []
        self._next_id = 1
        self._next_ver_id = 1

    @staticmethod
    def _same_layer(e: SlangEntryData, scope: str, tenant_id: str, user_id: str) -> bool:
        return (
            e.scope == scope and e.tenant_id == tenant_id and e.user_id == user_id
        )

    def _audit(
        self,
        *,
        entry_id: int,
        version: int,
        change_type: str,
        before: Optional[SlangEntryData],
        after: Optional[SlangEntryData],
        operator: str = "",
    ) -> None:
        self._versions.append(
            SlangVersionData(
                id=self._next_ver_id,
                entry_id=entry_id,
                version=version,
                change_type=change_type,
                before_json=before.model_dump(mode="json") if before else None,
                after_json=after.model_dump(mode="json") if after else None,
                operator=operator,
                created_at=_now(),
            )
        )
        self._next_ver_id += 1

    async def list_layers(
        self, tenant_id: str = "", user_id: str = ""
    ) -> Dict[str, List[SlangEntryData]]:
        result: Dict[str, List[SlangEntryData]] = {
            SCOPE_INDUSTRY: [],
            SCOPE_TENANT: [],
            SCOPE_USER: [],
        }
        for e in self._entries:
            if e.status != STATUS_PUBLISHED:
                continue
            if e.scope == SCOPE_INDUSTRY:
                result[SCOPE_INDUSTRY].append(e)
            elif e.scope == SCOPE_TENANT and e.tenant_id == tenant_id:
                result[SCOPE_TENANT].append(e)
            elif (
                e.scope == SCOPE_USER
                and e.tenant_id == tenant_id
                and e.user_id == user_id
            ):
                result[SCOPE_USER].append(e)
        return result

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
        rows = [
            e
            for e in self._entries
            if (scope is None or e.scope == scope)
            and (tenant_id is None or e.tenant_id == tenant_id)
            and (user_id is None or e.user_id == user_id)
            and (entry_type is None or e.entry_type == entry_type)
            and (status is None or e.status == status)
            and (source is None or e.source == source)
            and (
                not keyword
                or keyword in e.term
                or keyword in (e.standard or "")
                or keyword in (e.remark or "")
            )
        ]
        rows.sort(key=lambda e: (e.scope, -e.priority, -(e.id or 0)))
        total = len(rows)
        start = max(0, offset)
        return rows[start : start + max(1, limit)], total

    async def get(self, entry_id: int) -> Optional[SlangEntryData]:
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    async def find(self, entry: SlangEntryData) -> Optional[SlangEntryData]:
        for e in self._entries:
            if (
                e.scope == entry.scope
                and e.tenant_id == entry.tenant_id
                and e.user_id == entry.user_id
                and e.entry_type == entry.entry_type
                and e.term == entry.term
            ):
                return e
        return None

    async def add(self, entry: SlangEntryData) -> SlangEntryData:
        async with self._lock:
            entry.id = self._next_id
            self._next_id += 1
            entry.version = 1
            entry.created_at = entry.updated_at = _now()
            self._entries.append(entry)
            self._audit(
                entry_id=entry.id,
                version=1,
                change_type="create",
                before=None,
                after=entry,
                operator=entry.created_by,
            )
            return entry

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
        async with self._lock:
            e = await self.get(entry_id)
            if e is None:
                return None
            before = e.model_copy(deep=True)
            if term is not None:
                e.term = term
            if standard is not None:
                e.standard = standard
            if entry_type is not None:
                e.entry_type = entry_type
            if dim_value is not None:
                e.dim_value = dim_value
            if rewrite_mode is not None:
                e.rewrite_mode = rewrite_mode
            if priority is not None:
                e.priority = priority
            if remark is not None:
                e.remark = remark
            change_type = "update"
            if status is not None and status != e.status:
                e.status = status
                change_type = "publish" if status == STATUS_PUBLISHED else (
                    "disable" if status == STATUS_DISABLED else "update"
                )
            e.version += 1
            e.updated_by = operator or e.updated_by
            e.updated_at = _now()
            self._audit(
                entry_id=entry_id,
                version=e.version,
                change_type=change_type,
                before=before,
                after=e,
                operator=operator,
            )
            return e

    async def delete(self, entry_id: int, operator: str = "") -> bool:
        async with self._lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    self._entries.pop(i)
                    self._audit(
                        entry_id=entry_id,
                        version=e.version,
                        change_type="delete",
                        before=e,
                        after=None,
                        operator=operator,
                    )
                    return True
            return False

    async def count_by_scope(self) -> Dict[str, int]:
        counts = {s: 0 for s in ALL_SCOPES}
        for e in self._entries:
            counts[e.scope] = counts.get(e.scope, 0) + 1
        return counts

    async def count(self, status: Optional[str] = None) -> int:
        return len([e for e in self._entries if status is None or e.status == status])

    async def add_version(self, version: SlangVersionData) -> SlangVersionData:
        version.id = self._next_ver_id
        self._next_ver_id += 1
        version.created_at = _now()
        self._versions.append(version)
        return version

    async def list_versions(self, entry_id: int) -> List[SlangVersionData]:
        rows = [v for v in self._versions if v.entry_id == entry_id]
        rows.sort(key=lambda v: -v.version)
        return rows

    async def bump_hit(self, entry_ids: List[int]) -> int:
        ids = {i for i in entry_ids if i}
        n = 0
        for e in self._entries:
            if e.id in ids:
                e.hit_count += 1
                n += 1
        return n

    async def replace_scope_entries(
        self, scope: str, tenant_id: str, user_id: str, entries: List[SlangEntryData]
    ) -> int:
        async with self._lock:
            self._entries = [
                e
                for e in self._entries
                if not (
                    self._same_layer(e, scope, tenant_id, user_id)
                    and e.source == SOURCE_BUILTIN
                )
            ]
            for e in entries:
                e.id = self._next_id
                self._next_id += 1
                e.scope = scope
                e.tenant_id = tenant_id
                e.user_id = user_id
                e.source = SOURCE_BUILTIN
                e.status = STATUS_PUBLISHED
                e.version = 1
                e.created_at = e.updated_at = _now()
                self._entries.append(e)
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
        eid = len(self._rewrite_events) + 1
        self._rewrite_events.append(
            {
                "id": eid,
                "created_at": _now(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": session_id,
                "message_id": message_id,
                "query_hash": query_hash,
                "query_text": query_text[:512],
                "hit_count": hit_count,
                "is_rewritten": is_rewritten,
                "feedback": None,
                "note": "",
            }
        )
        return eid

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
        value = 1 if is_correct else -1
        row = None
        if message_id:
            for ev in reversed(self._rewrite_events):
                if ev["message_id"] == message_id:
                    row = ev
                    break
        if row is None and query_hash:
            for ev in reversed(self._rewrite_events):
                if ev["query_hash"] == query_hash:
                    row = ev
                    break
        if cancel:
            if row is not None:
                row["feedback"] = None
                if note:
                    row["note"] = note[:255]
                return row["id"]
            return 0
        if row is not None:
            row["feedback"] = value
            row["note"] = note[:255]
            if session_id:
                row["session_id"] = session_id
            if message_id:
                row["message_id"] = message_id
            return row["id"]
        eid = len(self._rewrite_events) + 1
        self._rewrite_events.append(
            {
                "id": eid,
                "created_at": _now(),
                "tenant_id": "",
                "user_id": "",
                "session_id": session_id,
                "message_id": message_id,
                "query_hash": query_hash,
                "query_text": "",
                "hit_count": 0,
                "is_rewritten": False,
                "feedback": value,
                "note": note[:255],
            }
        )
        return eid

    async def get_rewrite_feedback_by_messages(
        self, message_ids: List[str]
    ) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for ev in self._rewrite_events:
            if ev["message_id"] in message_ids and ev["feedback"] is not None:
                result[ev["message_id"]] = ev["feedback"]
        return result

    async def query_rewrite_events(
        self, since: Optional[datetime] = None, limit: int = 10000
    ) -> List[Dict[str, Any]]:
        rows = list(self._rewrite_events)
        if since is not None:
            rows = [r for r in rows if r["created_at"] >= since]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[:limit]

    async def count_rewrite_events(self, since: Optional[datetime] = None) -> int:
        if since is None:
            return len(self._rewrite_events)
        return sum(1 for r in self._rewrite_events if r["created_at"] >= since)


class InMemoryTraceRepository:
    """对话观测 trace 内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: List[TraceData] = []

    async def add(self, record: TraceData) -> TraceData:
        async with self._lock:
            record.id = len(self._records) + 1
            record.created_at = _now()
            self._records.append(record)
            return record

    async def list_recent(self, limit: int = 50) -> List[TraceData]:
        return sorted(
            self._records, key=lambda r: r.created_at or _now(), reverse=True
        )[:limit]

    async def get(self, session_id: str, round_id: str) -> Optional[TraceData]:
        for r in self._records:
            if r.session_id == session_id and r.round_id == round_id:
                return r
        return None

    async def aggregate(self, limit: int = 500) -> Dict[str, Any]:
        records = sorted(
            self._records, key=lambda r: r.created_at or _now(), reverse=True
        )[:limit]
        return aggregate_traces(records)

    # ---- 过滤 / 分页（观测专页） ----
    def _filter(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
    ) -> List[TraceData]:
        out = []
        kw = (keyword or "").strip().lower()
        for r in self._records:
            if session_id and r.session_id != session_id:
                continue
            if intent and r.intent != intent:
                continue
            if has_error is not None and bool(r.has_error) != has_error:
                continue
            if kw:
                haystack = " ".join(
                    [
                        str(r.intent or ""),
                        str((r.trace or {}).get("final_answer") or ""),
                        " ".join(r.skills or []),
                        str((r.trace or {}).get("rewritten_query") or ""),
                    ]
                ).lower()
                if kw not in haystack:
                    continue
            out.append(r)
        return sorted(out, key=lambda r: r.created_at or _now(), reverse=True)

    async def query(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[TraceData], int]:
        hits = self._filter(
            session_id=session_id, intent=intent, has_error=has_error, keyword=keyword
        )
        return hits[offset : offset + limit], len(hits)

    async def aggregate_filtered(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        hits = self._filter(
            session_id=session_id, intent=intent, has_error=has_error, keyword=keyword
        )[:limit]
        return aggregate_traces(hits)

    # ---- FR11 成本归集（与 SQL 实现同口径：未知分组回落 model） ----
    async def cost_breakdown(
        self,
        *,
        group_by: str = "model",
        since: Optional[Any] = None,
        until: Optional[Any] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        def key_of(r: TraceData) -> str:
            if group_by == "user":
                return r.user_id or ""
            if group_by == "tenant":
                return r.tenant_id or ""
            if group_by == "intent":
                return r.intent or ""
            if group_by == "day":
                return (r.created_at or _now()).strftime("%Y-%m-%d")
            return r.model or ""

        buckets: Dict[str, Dict[str, Any]] = {}
        for r in self._records:
            if since is not None and (r.created_at or _now()) < since:
                continue
            if until is not None and (r.created_at or _now()) >= until:
                continue
            b = buckets.setdefault(
                key_of(r),
                {
                    "key": key_of(r),
                    "rounds": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                },
            )
            b["rounds"] += 1
            b["prompt_tokens"] += int(r.prompt_tokens or 0)
            b["completion_tokens"] += int(r.completion_tokens or 0)
            b["total_tokens"] += int(r.total_tokens or 0)
            b["cost"] += float(r.cost or 0.0)
        rows = sorted(buckets.values(), key=lambda x: x["cost"], reverse=True)
        for b in rows:
            b["cost"] = round(b["cost"], 6)
        return rows[: max(limit, 1)]


class InMemorySpanRepository:
    """OpenTelemetry span 内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._spans: List[SpanData] = []

    async def add_many(self, spans: List[SpanData]) -> int:
        if not spans:
            return 0
        async with self._lock:
            for s in spans:
                s.id = len(self._spans) + 1
                s.created_at = _now()
                self._spans.append(s)
            return len(spans)

    async def list_by_round(self, session_id: str, round_id: str) -> List[SpanData]:
        rows = [
            s
            for s in self._spans
            if s.session_id == session_id and s.round_id == round_id
        ]
        return sorted(rows, key=lambda s: (s.start_time or _now(), s.id or 0))

    async def list_by_session(
        self, session_id: str, limit: int = 500
    ) -> List[SpanData]:
        rows = [s for s in self._spans if s.session_id == session_id]
        rows.sort(key=lambda s: (s.start_time or _now(), s.id or 0))
        return rows[-limit:]

    async def list_recent(self, limit: int = 200) -> List[SpanData]:
        return sorted(self._spans, key=lambda s: s.id or 0, reverse=True)[:limit]

    async def aggregate(self, limit: int = 2000) -> Dict[str, Any]:
        return aggregate_spans(self._spans[-limit:])


class InMemoryFewShotRepository:
    """few-shot 样本库内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: Dict[int, FewShotData] = {}  # id -> 样本
        self._by_message: Dict[str, int] = {}  # message_id -> id（点赞幂等用）
        self._seq = 0

    async def add(self, example: FewShotData) -> FewShotData:
        async with self._lock:
            if example.message_id and example.message_id in self._by_message:
                cur = self._items[self._by_message[example.message_id]]
                cur.score = (cur.score or 0) + 1
                return cur
            self._seq += 1
            obj = FewShotData(
                id=self._seq,
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
                created_at=_now(),
            )
            self._items[obj.id] = obj
            if obj.message_id:
                self._by_message[obj.message_id] = obj.id
            return obj

    async def list_all(self, status: Optional[str] = None) -> List[FewShotData]:
        items = list(self._items.values())
        if status:
            items = [i for i in items if i.status == status]
        items.sort(key=lambda i: i.created_at or _now(), reverse=True)
        return items

    def _filter(
        self,
        *,
        category: Optional[str] = None,
        intent: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[FewShotData]:
        kw = (keyword or "").strip().lower()
        out: List[FewShotData] = []
        for i in self._items.values():
            if category and i.category != category:
                continue
            if intent and i.intent != intent:
                continue
            if source and i.source != source:
                continue
            if status and i.status != status:
                continue
            if kw:
                haystack = f"{i.question} {i.answer} {i.tags}".lower()
                if kw not in haystack:
                    continue
            out.append(i)
        out.sort(key=lambda i: i.created_at or _now(), reverse=True)
        return out

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
        hits = self._filter(
            category=category, intent=intent, source=source,
            status=status, keyword=keyword,
        )
        return hits[max(offset, 0) : max(offset, 0) + max(limit, 1)], len(hits)

    async def get_by_message(self, message_id: str) -> Optional[FewShotData]:
        if not message_id:
            return None
        item_id = self._by_message.get(message_id)
        return self._items.get(item_id) if item_id else None

    async def delete_by_message(self, message_id: str) -> bool:
        """按消息撤销点赞样本（用户再点一次点赞=取消）。"""
        item_id = self._by_message.pop(message_id, None) if message_id else None
        if item_id is None:
            return False
        self._items.pop(item_id, None)
        return True

    async def get_by_id(self, item_id: int) -> Optional[FewShotData]:
        return self._items.get(item_id)

    async def find_by_question(
        self, question: str, category: Optional[str] = None
    ) -> Optional[FewShotData]:
        for i in self._items.values():
            if i.question == question and (not category or i.category == category):
                return i
        return None

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
        obj = self._items.get(item_id)
        if obj is None:
            return None
        async with self._lock:
            for name, value in (
                ("question", question), ("answer", answer), ("intent", intent),
                ("category", category), ("tags", tags), ("weight", weight),
                ("status", status),
            ):
                if value is not None:
                    setattr(obj, name, value)
            return obj

    async def delete(self, item_id: int) -> bool:
        async with self._lock:
            obj = self._items.pop(item_id, None)
            if obj is None:
                return False
            if obj.message_id:
                self._by_message.pop(obj.message_id, None)
            return True

    async def count(self, status: Optional[str] = None, source: Optional[str] = None) -> int:
        return len(self._filter(status=status, source=source))


class InMemoryBadCaseRepository:
    """点踩样本库（bad case）内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[BadCaseData] = []
        self._seq = 0

    async def add(self, case: BadCaseData) -> BadCaseData:
        async with self._lock:
            self._seq += 1
            obj = BadCaseData(
                id=self._seq,
                session_id=case.session_id,
                message_id=case.message_id,
                intent=case.intent,
                question=case.question,
                answer=case.answer,
                reason=case.reason,
                created_at=_now(),
            )
            self._items.append(obj)
            return obj

    async def list_recent(self, limit: int = 50) -> List[BadCaseData]:
        return self._items[-limit:][::-1]

    async def get_by_message(self, message_id: str) -> Optional[BadCaseData]:
        for c in self._items:
            if c.message_id == message_id:
                return c
        return None

    async def delete_by_message(self, message_id: str) -> bool:
        """按消息撤销 bad-case（用户再点一次点踩=取消）。"""
        for idx, c in enumerate(self._items):
            if c.message_id == message_id:
                self._items.pop(idx)
                return True
        return False

    async def count(self) -> int:
        """点踩总数（FR11 采纳率分母）。"""
        return len(self._items)


class InMemoryDomainMemoryRepository:
    """领域知识记忆（L3）内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[DomainMemoryData] = []
        self._seq = 0

    async def add(self, item: DomainMemoryData) -> DomainMemoryData:
        async with self._lock:
            self._seq += 1
            obj = DomainMemoryData(
                id=self._seq,
                domain=item.domain,
                type=item.type,
                question=item.question,
                answer=item.answer,
                intent=item.intent,
                tags=item.tags,
                weight=item.weight,
                status=item.status,
                created_by=item.created_by,
                created_at=_now(),
            )
            self._items.append(obj)
            return obj

    async def list_all(self) -> List[DomainMemoryData]:
        return list(self._items)

    async def list_approved(
        self, domain: Optional[str] = None, type: Optional[str] = None
    ) -> List[DomainMemoryData]:
        out = [d for d in self._items if d.status == "approved"]
        if domain:
            out = [d for d in out if d.domain == domain]
        if type:
            out = [d for d in out if d.type == type]
        return sorted(out, key=lambda d: d.weight, reverse=True)

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
        kw = (keyword or "").strip().lower()
        hits: List[DomainMemoryData] = []
        for d in self._items:
            if domain and d.domain != domain:
                continue
            if type and d.type != type:
                continue
            if status and d.status != status:
                continue
            if kw:
                haystack = f"{d.question} {d.answer} {d.tags}".lower()
                if kw not in haystack:
                    continue
            hits.append(d)
        hits.sort(key=lambda d: d.created_at or _now(), reverse=True)
        return hits[max(offset, 0) : max(offset, 0) + max(limit, 1)], len(hits)

    async def get_by_id(self, item_id: int) -> Optional[DomainMemoryData]:
        for d in self._items:
            if d.id == item_id:
                return d
        return None

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
        obj = await self.get_by_id(item_id)
        if obj is None:
            return None
        for name, value in (
            ("domain", domain), ("type", type), ("question", question),
            ("answer", answer), ("intent", intent), ("tags", tags),
            ("weight", weight), ("status", status),
        ):
            if value is not None:
                setattr(obj, name, value)
        return obj

    async def delete(self, item_id: int) -> None:
        async with self._lock:
            self._items = [d for d in self._items if d.id != item_id]

    async def count_by_type(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self._items:
            out[d.type or ""] = out.get(d.type or "", 0) + 1
        return out


class InMemoryTenantRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tenants: Dict[str, TenantData] = {}

    async def add(self, tenant: TenantData) -> TenantData:
        async with self._lock:
            if not tenant.id:
                tenant.id = _new_id()
            tenant.created_at = tenant.created_at or _now()
            self._tenants[tenant.id] = tenant
            return tenant

    async def get(self, tenant_id: str) -> Optional[TenantData]:
        return self._tenants.get(tenant_id)

    async def get_by_code(self, code: str) -> Optional[TenantData]:
        for t in self._tenants.values():
            if t.code == code:
                return t
        return None

    async def list_all(self) -> List[TenantData]:
        return sorted(self._tenants.values(), key=lambda t: t.created_at)

    async def update(
        self, tenant_id: str, name: Optional[str] = None, status: Optional[str] = None
    ) -> Optional[TenantData]:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            return None
        if name is not None:
            tenant.name = name
        if status is not None:
            tenant.status = status
        return tenant

    async def delete(self, tenant_id: str) -> bool:
        return self._tenants.pop(tenant_id, None) is not None


class InMemoryRoleRepository:
    """角色仓储内存实现（降级 / 测试用）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._roles: Dict[str, RoleData] = {}

    async def add(self, role: RoleData) -> RoleData:
        async with self._lock:
            role.created_at = role.created_at or _now()
            self._roles[role.name] = role
            return role

    async def get(self, name: str) -> Optional[RoleData]:
        return self._roles.get(name)

    async def list_all(self) -> List[RoleData]:
        return sorted(self._roles.values(), key=lambda r: r.created_at or _now())

    async def update(
        self,
        name: str,
        *,
        label: Optional[str] = None,
        description: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        status: Optional[str] = None,
    ) -> Optional[RoleData]:
        role = self._roles.get(name)
        if role is None:
            return None
        if label is not None:
            role.label = label
        if description is not None:
            role.description = description
        if permissions is not None:
            role.permissions = list(permissions)
        if status is not None:
            role.status = status
        return role

    async def delete(self, name: str) -> bool:
        return self._roles.pop(name, None) is not None

    async def count(self) -> int:
        return len(self._roles)


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._users: Dict[str, UserData] = {}
        self._hashes: Dict[str, str] = {}
    async def add(self, user: UserData, password_hash: str) -> UserData:
        async with self._lock:
            if not user.id:
                user.id = _new_id()
            user.created_at = user.created_at or _now()
            user.updated_at = user.updated_at or _now()
            self._users[user.id] = user
            self._hashes[user.id] = password_hash
            return user

    async def get(self, user_id: str) -> Optional[UserData]:
        return self._users.get(user_id)

    async def get_by_username(self, username: str) -> Optional[UserData]:
        for u in self._users.values():
            if u.username == username:
                return u
        return None

    async def get_password_hash(self, user_id: str) -> Optional[str]:
        return self._hashes.get(user_id)

    async def list_all(self, tenant_id: Optional[str] = None) -> List[UserData]:
        items = list(self._users.values())
        if tenant_id:
            items = [u for u in items if u.tenant_id == tenant_id]
        return sorted(items, key=lambda u: u.created_at or _now())

    async def update(
        self,
        user_id: str,
        display_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[UserData]:
        user = self._users.get(user_id)
        if user is None:
            return None
        if display_name is not None:
            user.display_name = display_name
        if tenant_id is not None:
            user.tenant_id = tenant_id
        if role is not None:
            user.role = role
        if status is not None:
            user.status = status
        user.updated_at = _now()
        return user

    async def set_password(self, user_id: str, password_hash: str) -> bool:
        if user_id not in self._users:
            return False
        self._hashes[user_id] = password_hash
        return True

    async def touch_login(self, user_id: str) -> None:
        user = self._users.get(user_id)
        if user is not None:
            user.last_login_at = _now()

    async def delete(self, user_id: str) -> bool:
        self._hashes.pop(user_id, None)
        return self._users.pop(user_id, None) is not None

    async def count(self) -> int:
        return len(self._users)


class InMemoryUserPreferenceRepository:
    """用户偏好（L2）内存实现：键为 (user_id, pref_key)，upsert 覆盖。"""

    def __init__(self) -> None:
        self._items: Dict[Tuple[str, str], UserPreferenceData] = {}

    async def upsert(self, pref: UserPreferenceData) -> UserPreferenceData:
        key = (pref.user_id, pref.key)
        existing = self._items.get(key)
        now = _now()
        if existing is None:
            if not pref.id:
                pref.id = len(self._items) + 1
            pref.created_at = pref.created_at or now
            pref.updated_at = now
            self._items[key] = pref
            return pref
        existing.tenant_id = pref.tenant_id
        existing.value = pref.value
        existing.source = pref.source
        existing.confidence = pref.confidence
        existing.status = pref.status
        existing.updated_at = now
        return existing

    async def list_by_user(
        self, user_id: str, status: Optional[str] = None
    ) -> List[UserPreferenceData]:
        items = [p for (uid, _), p in self._items.items() if uid == user_id]
        if status:
            items = [p for p in items if p.status == status]
        items.sort(key=lambda p: p.updated_at or _now(), reverse=True)
        return items

    async def list_all(
        self,
        *,
        tenant_id: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[UserPreferenceData]:
        """全平台偏好（管理面 L2 视角）。"""
        items = list(self._items.values())
        if tenant_id:
            items = [p for p in items if p.tenant_id == tenant_id]
        if source:
            items = [p for p in items if p.source == source]
        if status:
            items = [p for p in items if p.status == status]
        if user_id:
            items = [p for p in items if p.user_id == user_id]
        items.sort(key=lambda p: p.updated_at or _now(), reverse=True)
        return items[: max(limit, 1)]

    async def get(self, user_id: str, key: str) -> Optional[UserPreferenceData]:
        return self._items.get((user_id, key))

    async def delete(self, user_id: str, key: str) -> bool:
        return self._items.pop((user_id, key), None) is not None

    async def delete_by_id(self, pref_id: int) -> bool:
        for k, p in list(self._items.items()):
            if p.id == pref_id:
                self._items.pop(k, None)
                return True
        return False

    async def clear_user(self, user_id: str) -> int:
        keys = [k for k in self._items if k[0] == user_id]
        for k in keys:
            self._items.pop(k, None)
        return len(keys)

    async def count(self) -> int:
        return len(self._items)


class InMemorySemanticRegistryRepository:
    """语义注册中心内存实现（与 MySQL 版同接口，测试 / 降级用）。"""

    def __init__(self) -> None:
        self._models: List[Dict[str, Any]] = []  # 含 dimensions/measures 嵌套
        self._metrics: List[Dict[str, Any]] = []
        self._versions: List[Dict[str, Any]] = []
        self._dimension_versions: List[Dict[str, Any]] = []
        self._next_id = 1
        self._next_dim_id = 1

    @staticmethod
    def _kw_hit(text: str, keyword: str) -> bool:
        return keyword.lower() in (text or "").lower()

    # ---------- 租户作用域（与 MySQL 版**同语义**，别各写各的）----------
    #
    # 两套语义刻意分开（读得到 ≠ 改得动）：
    #   - 可见性（读）：None = 不过滤｜"" = 仅全局｜"t" = 全局 + 本租户；
    #   - 归属（写 / 按 key 精确定位）：None = 历史行为（任意行）｜否则**精确等于**。
    # 维度/度量没有自己的 tenant_id，租户归属**继承宿主模型**。

    @staticmethod
    def _visible(tid: Optional[str], row_tid: str) -> bool:
        if tid is None:
            return True
        t = (tid or "").strip()
        rt = row_tid or ""
        return rt == "" or rt == t

    @staticmethod
    def _owned(tid: Optional[str], row_tid: str) -> bool:
        if tid is None:
            return True
        return (row_tid or "") == (tid or "").strip()

    def _visible_models(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        return [m for m in self._models if self._visible(tenant_id, m.get("tenant_id") or "")]

    def _owned_models(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        return [m for m in self._models if self._owned(tenant_id, m.get("tenant_id") or "")]

    def _find_metric_scoped(
        self, metric_key: str, tenant_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """按 (metric_key, 归属) 精确定位；同 key 多租户时归属优先全局之后按插入序。"""
        hits = [m for m in self._metrics if m.get("metric_key") == metric_key]
        if not hits:
            return None
        if tenant_id is None:
            return hits[0]
        t = (tenant_id or "").strip()
        for m in hits:
            if (m.get("tenant_id") or "") == t:
                return m
        return None

    def _find_dimension_scoped(
        self, dimension_key: str, tenant_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """按 (dimension_key, 宿主模型归属) 精确定位；维度租户继承宿主模型。"""
        allowed_keys = None
        if tenant_id is not None:
            allowed_keys = {
                (m.get("model_key") or "")
                for m in self._owned_models(tenant_id)
            }
        for m in self._models:
            if allowed_keys is not None and (m.get("model_key") or "") not in allowed_keys:
                continue
            for d in m.get("dimensions") or []:
                if d.get("dimension_key") == dimension_key:
                    return d
        return None

    # ---------- 列表 / 详情 ----------

    async def list_models(
        self,
        *,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows = [
            m
            for m in self._models
            if (not status or m.get("status") == status)
            and self._visible(tenant_id, m.get("tenant_id") or "")
            and (
                not keyword
                or self._kw_hit(m.get("model_key", ""), keyword)
                or self._kw_hit(m.get("name", ""), keyword)
                or self._kw_hit(m.get("description", ""), keyword)
            )
        ]
        total = len(rows)
        items = []
        for m in rows[offset : offset + limit]:
            item = dict(m)
            item["dimension_count"] = len(m.get("dimensions") or [])
            item["measure_count"] = len(m.get("measures") or [])
            item.pop("dimensions", None)
            item.pop("measures", None)
            items.append(item)
        return items, total

    async def get_model(
        self, model_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        # 与 MySQL 版同口径：详情按"可见性"取，先本租户、再回落全局（全局对租户只读）
        m = self._find_model_scoped(model_key, tenant_id)
        if m is None:
            m = self._find_model_scoped(model_key, "")
        return copy.deepcopy(m) if m else None

    def _find_model_scoped(
        self, model_key: str, tenant_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        hits = [x for x in self._models if x.get("model_key") == model_key]
        if not hits:
            return None
        if tenant_id is None:
            return hits[0]
        t = (tenant_id or "").strip()
        for x in hits:
            if (x.get("tenant_id") or "") == t:
                return x
        return None

    async def list_dimensions(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows: List[Dict[str, Any]] = []
        for m in self._visible_models(tenant_id):
            if model_key and m.get("model_key") != model_key:
                continue
            for d in m.get("dimensions") or []:
                if keyword and not (
                    self._kw_hit(d.get("name", ""), keyword)
                    or self._kw_hit(d.get("dimension_key", ""), keyword)
                ):
                    continue
                rows.append({**d, "model_key": m.get("model_key", "")})
        total = len(rows)
        return rows[offset : offset + limit], total

    async def list_measures(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows: List[Dict[str, Any]] = []
        for m in self._visible_models(tenant_id):
            if model_key and m.get("model_key") != model_key:
                continue
            for x in m.get("measures") or []:
                if keyword and not (
                    self._kw_hit(x.get("name", ""), keyword)
                    or self._kw_hit(x.get("measure_key", ""), keyword)
                ):
                    continue
                rows.append({**x, "model_key": m.get("model_key", "")})
        total = len(rows)
        return rows[offset : offset + limit], total

    async def list_metrics(
        self,
        *,
        status: Optional[str] = None,
        metric_type: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows = [
            m
            for m in self._metrics
            if (not status or m.get("status") == status)
            and (not metric_type or m.get("metric_type") == metric_type)
            and self._visible(tenant_id, m.get("tenant_id") or "")
            and (
                not keyword
                or self._kw_hit(m.get("name", ""), keyword)
                or self._kw_hit(m.get("metric_key", ""), keyword)
                or any(self._kw_hit(a, keyword) for a in m.get("aliases") or [])
            )
        ]
        total = len(rows)
        return [dict(m) for m in rows[offset : offset + limit]], total

    async def get_metric(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        row = self._find_metric_scoped(metric_key, tenant_id)
        if row is None:
            row = self._find_metric_scoped(metric_key, "")  # 回落全局（只读）
        if row is None:
            return None
        payload = dict(row)
        # 宿主模型（simple：按 measure 反查）
        hosts: List[Dict[str, Any]] = []
        if row.get("metric_type") == "simple":
            measure = (row.get("type_params") or {}).get("measure")
            for m in self._models:
                if any(x.get("measure_key") == measure for x in m.get("measures") or []):
                    hosts.append({"model_key": m.get("model_key"), "name": m.get("name")})
        payload["host_models"] = hosts
        return payload

    # ---------- 统计 / facets ----------

    async def stats(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        vmodels = self._visible_models(tenant_id)
        vmetrics = [
            m for m in self._metrics if self._visible(tenant_id, m.get("tenant_id") or "")
        ]
        dims = sum(len(m.get("dimensions") or []) for m in vmodels)
        measures = sum(len(m.get("measures") or []) for m in vmodels)

        def _by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for it in items:
                k = it.get(key) or ""
                out[k] = out.get(k, 0) + 1
            return out

        return {
            "models": len(vmodels),
            "metrics": len(vmetrics),
            "dimensions": dims,
            "measures": measures,
            "metrics_by_status": _by(vmetrics, "status"),
            "metrics_by_type": _by(vmetrics, "metric_type"),
            "models_by_status": _by(vmodels, "status"),
        }

    async def facets(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        vmodels = self._visible_models(tenant_id)
        vmetrics = [
            m for m in self._metrics if self._visible(tenant_id, m.get("tenant_id") or "")
        ]
        return {
            "metric_types": sorted({m.get("metric_type", "") for m in vmetrics}),
            "statuses": sorted({m.get("status", "") for m in vmetrics}),
            "model_keys": [m.get("model_key", "") for m in vmodels],
            "units": sorted({m.get("unit", "") for m in vmetrics if m.get("unit")}),
        }

    # ---------- 种子导入（幂等） ----------

    async def import_seed(
        self, seed: Dict[str, Any], *, operator: str = "system"
    ) -> Dict[str, Any]:
        models_imported = models_skipped = metrics_imported = metrics_skipped = 0
        now = datetime.now().isoformat()
        for raw in seed.get("models") or []:
            key = raw.get("model_key") or ""
            if not key:
                continue
            if any(m.get("model_key") == key for m in self._models):
                models_skipped += 1
                continue
            item = dict(raw)
            item.setdefault("status", "published")
            item.setdefault("source", "builtin")
            item.setdefault("version", 1)
            item["created_at"] = now
            item["updated_at"] = None
            # 维度记录补齐 id/version（维度写路径需要稳定身份）
            for d in item.get("dimensions") or []:
                d.setdefault("id", self._next_dim_id)
                self._next_dim_id += 1
                d.setdefault("version", 1)
                d.setdefault("status", "published")
                d.setdefault("model_key", key)
                d.setdefault("approved_by", "")
            self._models.append(item)
            models_imported += 1
        for raw in seed.get("metrics") or []:
            key = raw.get("metric_key") or ""
            if not key:
                continue
            if any(m.get("metric_key") == key for m in self._metrics):
                metrics_skipped += 1
                continue
            item = dict(raw)
            item.setdefault("status", "published")
            item.setdefault("source", "builtin")
            item.setdefault("version", 1)
            item["id"] = self._next_id
            self._next_id += 1
            item["created_at"] = now
            item["updated_at"] = None
            self._metrics.append(item)
            self._versions.append(
                {
                    "metric_id": item["id"],
                    "version": 1,
                    "change_type": "import",
                    "after_json": dict(item),
                    "operator": operator,
                }
            )
            metrics_imported += 1
        return {
            "models_imported": models_imported,
            "models_skipped": models_skipped,
            "metrics_imported": metrics_imported,
            "metrics_skipped": metrics_skipped,
        }

    async def count_models(self) -> int:
        return len(self._models)

    # ---------- 写路径：指标（P2）----------

    def _find_metric(self, metric_key: str) -> Optional[Dict[str, Any]]:
        return next((m for m in self._metrics if m.get("metric_key") == metric_key), None)

    async def create_metric_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``tenant_id`` 非 None 时**强制覆盖**载荷里的归属（同 MySQL 版）。"""
        resolved_tid = (
            (tenant_id or "").strip()
            if tenant_id is not None
            else (defn.get("tenant_id") or "")
        )
        key = defn.get("metric_key") or derive_metric_key(defn)
        base, n = key, 2
        # 唯一性是 (scope, tenant_id, key)：同 key 在**别租户**下可以存在（租户覆盖全局）
        while self._find_metric_scoped(key, resolved_tid) is not None:
            key = f"{base}_{n}"
            n += 1
        item = {
            "id": self._next_id,
            "tenant_id": resolved_tid,
            "scope": defn.get("scope") or "industry",
            "metric_key": key,
            "name": defn.get("name") or key,
            "metric_type": defn.get("metric_type") or "simple",
            "type_params": dict(defn.get("type_params") or {}),
            "aliases": list(defn.get("aliases") or []),
            "unit": defn.get("unit") or "",
            "description": defn.get("description") or "",
            "scale": float(defn.get("scale") or 1.0),
            "round": int(defn.get("round") or 4),
            "granularities": list(defn.get("granularities") or []),
            "pop_types": list(defn.get("pop_types") or []),
            "status": DRAFT,
            "version": 1,
            "source": "user",
            "owner": defn.get("owner") or "",
            "approved_by": "",
            "created_at": datetime.now().isoformat(),
            "updated_at": None,
        }
        self._next_id += 1
        self._metrics.append(item)
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": 1,
                "change_type": "create",
                "before_json": None,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": "",
                "created_at": item["created_at"],
            }
        )
        return dict(item)

    async def update_metric(
        self,
        metric_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None:
            return None
        if item["status"] not in (DRAFT, REVIEW):
            raise ValueError(
                f"只能编辑草稿/待审指标（当前 {item['status']}）；"
                f"已发布/已废弃请先废弃或基于新版本重起草"
            )
        before = metric_core_defn(item)
        item["name"] = defn.get("name") or item["name"]
        item["metric_type"] = defn.get("metric_type") or item["metric_type"]
        item["type_params"] = defn.get("type_params") or item["type_params"]
        item["aliases"] = defn.get("aliases") or item["aliases"]
        item["unit"] = defn.get("unit") or ""
        item["description"] = defn.get("description") or ""
        item["scale"] = float(defn.get("scale") or item.get("scale") or 1.0)
        item["round"] = int(defn.get("round") or item.get("round") or 4)
        item["granularities"] = defn.get("granularities") or item["granularities"]
        item["pop_types"] = defn.get("pop_types") or item["pop_types"]
        item["owner"] = defn.get("owner") or item["owner"]
        item["updated_at"] = datetime.now().isoformat()
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": item["version"],
                "change_type": "update",
                "before_json": before,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": "",
                "created_at": item["updated_at"],
            }
        )
        return dict(item)

    async def submit_metric_review(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None:
            return None
        if not can_transition(item["status"], REVIEW):
            raise ValueError(f"非法状态转移：{item['status']} -> {REVIEW}")
        item["status"] = REVIEW
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": item["version"],
                "change_type": "submit",
                "before_json": None,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": "",
                "created_at": datetime.now().isoformat(),
            }
        )
        return dict(item)

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
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None:
            return {"ok": False, "error": "not_found"}
        if not can_transition(item["status"], PUBLISHED):
            return {"ok": False, "error": "illegal_transition", "current": item["status"]}
        defn = metric_core_defn(item)
        all_defs = {
            m["metric_key"]: {
                "metric_type": m["metric_type"],
                "type_params": m["type_params"],
                "status": m["status"],
            }
            for m in self._metrics
        }
        all_defs[metric_key] = {
            "metric_type": defn["metric_type"],
            "type_params": defn["type_params"],
            "status": defn.get("status", DRAFT),
        }
        measure_keys = {
            x.get("measure_key")
            for m in self._models
            for x in (m.get("measures") or [])
            if x.get("measure_key")
        }
        # PRD 5.9 闭环：把"待发布指标"并入 base 层构造副本层，使 ④ SQL dry-run / ⑥ 试跑
        # 能真正编译并试跑该指标（运行时层此刻尚未包含它）；构造失败则回落 base 层（降级 warning）。
        merged_layer = build_layer_with_metric(layer, item)
        result = await validate_metric_definition(
            defn,
            metric_key=metric_key,
            all_metric_defs=all_defs,
            measure_keys=measure_keys,
            layer=merged_layer or layer,
            engine=engine,
            deep=deep,
        )
        if not result.passed:
            return {"ok": False, "validation": result.to_dict()}
        before = defn
        item["status"] = PUBLISHED
        item["version"] = item["version"] + 1
        item["updated_at"] = datetime.now().isoformat()
        item["approved_by"] = approved_by
        # 落库 yaml_defn（PRD 5.9：发布指标生成 manifest 兼容口径 dict，供 KB 重建与可观测）
        item["yaml_defn"] = metric_to_yaml_defn(item)
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": item["version"],
                "change_type": "publish",
                "before_json": before,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": approved_by,
                "created_at": item["updated_at"],
            }
        )
        return {"ok": True, "metric": dict(item), "version": item["version"]}

    async def deprecate_metric(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None:
            return None
        if not can_transition(item["status"], DEPRECATED):
            raise ValueError(f"非法状态转移：{item['status']} -> {DEPRECATED}")
        item["status"] = DEPRECATED
        item["version"] = item["version"] + 1
        item["updated_at"] = datetime.now().isoformat()
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": item["version"],
                "change_type": "deprecate",
                "before_json": None,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": "",
                "created_at": item["updated_at"],
            }
        )
        return dict(item)

    async def rollback_metric(
        self,
        metric_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None:
            return None
        # 同一 revision 可能有多条审计（create/update/submit 均落在 v1），取最新一条。
        vid = None
        for v in self._versions:
            if v["metric_id"] == item["id"] and v["version"] == version:
                vid = v
        if vid is None:
            return None
        before = metric_core_defn(item)
        restored = vid.get("after_json") or {}
        item["name"] = restored.get("name") or item["name"]
        item["metric_type"] = restored.get("metric_type") or item["metric_type"]
        item["type_params"] = restored.get("type_params") or item["type_params"]
        item["aliases"] = restored.get("aliases") or item["aliases"]
        item["unit"] = restored.get("unit") or ""
        item["description"] = restored.get("description") or ""
        item["scale"] = float(restored.get("scale") or item.get("scale") or 1.0)
        item["round"] = int(restored.get("round") or item.get("round") or 4)
        item["granularities"] = restored.get("granularities") or item["granularities"]
        item["pop_types"] = restored.get("pop_types") or item["pop_types"]
        item["owner"] = restored.get("owner") or item["owner"]
        item["status"] = PUBLISHED
        item["version"] = item["version"] + 1
        item["updated_at"] = datetime.now().isoformat()
        item["approved_by"] = approved_by
        self._versions.append(
            {
                "metric_id": item["id"],
                "version": item["version"],
                "change_type": "rollback",
                "before_json": before,
                "after_json": metric_core_defn(item),
                "operator": operator,
                "approved_by": approved_by,
                "created_at": item["updated_at"],
            }
        )
        return dict(item)

    async def list_metric_versions(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None and tenant_id:
            item = self._find_metric_scoped(metric_key, "")  # 回落全局（只读）
        if item is None:
            return []
        return [
            {
                "version": v["version"],
                "change_type": v["change_type"],
                "operator": v["operator"],
                "approved_by": v.get("approved_by", ""),
                "created_at": v.get("created_at"),
                "after_json": v.get("after_json"),
            }
            for v in self._versions
            if v["metric_id"] == item["id"]
        ]

    async def get_metric_version(
        self, metric_key: str, version: int, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None and tenant_id:
            item = self._find_metric_scoped(metric_key, "")  # 回落全局（只读）
        if item is None:
            return None
        v = None
        for x in self._versions:
            if x["metric_id"] == item["id"] and x["version"] == version:
                v = x
        if v is None:
            return None
        return {
            "version": v["version"],
            "change_type": v["change_type"],
            "operator": v["operator"],
            "approved_by": v.get("approved_by", ""),
            "created_at": v.get("created_at"),
            "after_json": v.get("after_json"),
        }

    async def list_all_metric_defs(
        self, *, tenant_id: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """引用校验取值域 = 全局 ∪ 本租户；全局在前、租户在后（租户覆盖全局）。"""
        ordered = sorted(
            (
                m
                for m in self._metrics
                if self._visible(tenant_id, m.get("tenant_id") or "")
            ),
            key=lambda m: (m.get("tenant_id") or "", m.get("id") or 0),
        )
        return {
            m["metric_key"]: {
                "metric_type": m["metric_type"],
                "type_params": m["type_params"],
                "status": m["status"],
                "tenant_id": m.get("tenant_id") or "",
            }
            for m in ordered
        }

    async def list_all_measure_keys(
        self, *, tenant_id: Optional[str] = None
    ) -> Set[str]:
        return {
            x.get("measure_key")
            for m in self._visible_models(tenant_id)
            for x in (m.get("measures") or [])
            if x.get("measure_key")
        }

    async def get_metric_def(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        item = self._find_metric_scoped(metric_key, tenant_id)
        if item is None and tenant_id:
            item = self._find_metric_scoped(metric_key, "")  # 回落全局（只读）
        return metric_core_defn(item) if item is not None else None

    # ---------- 写路径：维度（P2，与指标对称）----------

    def _find_dimension(self, dimension_key: str, tenant_id: Optional[str] = None):
        """返回 (宿主模型, 维度)；``tenant_id`` 非 None 时宿主模型须**恰属**该租户，
        未命中再回落全局（全局维度对租户只读）。"""
        found = self._find_dimension_strict(dimension_key, tenant_id)
        if found[1] is None and tenant_id:
            found = self._find_dimension_strict(dimension_key, "")
        return found

    def _find_dimension_strict(self, dimension_key: str, tenant_id: Optional[str] = None):
        for m in self._owned_models(tenant_id) if tenant_id is not None else self._models:
            for d in m.get("dimensions") or []:
                if d.get("dimension_key") == dimension_key:
                    return m, d
        return None, None

    async def create_dimension_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        model_key = defn.get("model_key") or ""
        model = next(
            (
                m
                for m in (
                    self._owned_models(tenant_id)
                    if tenant_id is not None
                    else self._models
                )
                if m.get("model_key") == model_key
            ),
            None,
        )
        if model is None:
            raise ValueError(f"维度所属语义模型不存在或不属于当前租户：{model_key}")
        key = defn.get("dimension_key") or derive_dimension_key(defn)
        base, n = key, 2
        existing = {d.get("dimension_key") for d in model.get("dimensions") or []}
        while key in existing:
            key = f"{base}_{n}"
            n += 1
        now = datetime.now().isoformat()
        dim = {
            "id": self._next_dim_id,
            "dimension_key": key,
            "name": defn.get("name") or key,
            "dim_type": defn.get("dim_type") or "categorical",
            "expr": defn.get("expr") or "",
            "time_granularity": defn.get("time_granularity") or "day",
            "description": defn.get("description") or "",
            "status": DRAFT,
            "version": 1,
            "model_key": model_key,
            "model_id": id(model),
            "created_at": now,
            "updated_at": None,
            "approved_by": "",
        }
        self._next_dim_id += 1
        model.setdefault("dimensions", []).append(dim)
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": 1,
                "change_type": "create",
                "before_json": None,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": "",
                "created_at": now,
            }
        )
        return dict(dim)

    async def update_dimension(
        self,
        dimension_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return None
        if dim["status"] not in (DRAFT, REVIEW):
            raise ValueError(
                f"只能编辑草稿/待审维度（当前 {dim['status']}）；"
                f"已发布/已废弃请先废弃或基于新版本重起草"
            )
        before = dimension_core_defn(dim)
        dim["name"] = defn.get("name") or dim["name"]
        dim["dim_type"] = defn.get("dim_type") or dim["dim_type"]
        dim["expr"] = defn.get("expr") or dim["expr"]
        dim["time_granularity"] = defn.get("time_granularity") or dim["time_granularity"]
        dim["description"] = defn.get("description") or ""
        dim["updated_at"] = datetime.now().isoformat()
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": dim["version"],
                "change_type": "update",
                "before_json": before,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": "",
                "created_at": dim["updated_at"],
            }
        )
        return dict(dim)

    async def submit_dimension_review(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return None
        if not can_transition(dim["status"], REVIEW):
            raise ValueError(f"非法状态转移：{dim['status']} -> {REVIEW}")
        dim["status"] = REVIEW
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": dim["version"],
                "change_type": "submit",
                "before_json": None,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": "",
                "created_at": datetime.now().isoformat(),
            }
        )
        return dict(dim)

    async def publish_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return {"ok": False, "error": "not_found"}
        if not can_transition(dim["status"], PUBLISHED):
            return {"ok": False, "error": "illegal_transition", "current": dim["status"]}
        defn = dimension_core_defn(dim)
        result = validate_dimension_definition(defn)
        if not result.passed:
            return {"ok": False, "validation": result.to_dict()}
        dim["status"] = PUBLISHED
        dim["version"] = dim["version"] + 1
        dim["updated_at"] = datetime.now().isoformat()
        dim["approved_by"] = approved_by
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": dim["version"],
                "change_type": "publish",
                "before_json": None,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": approved_by,
                "created_at": dim["updated_at"],
            }
        )
        return {"ok": True, "dimension": dict(dim), "version": dim["version"]}

    async def deprecate_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return None
        if not can_transition(dim["status"], DEPRECATED):
            raise ValueError(f"非法状态转移：{dim['status']} -> {DEPRECATED}")
        dim["status"] = DEPRECATED
        dim["version"] = dim["version"] + 1
        dim["updated_at"] = datetime.now().isoformat()
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": dim["version"],
                "change_type": "deprecate",
                "before_json": None,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": "",
                "created_at": dim["updated_at"],
            }
        )
        return dict(dim)

    async def rollback_dimension(
        self,
        dimension_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return None
        vid = None
        for v in self._dimension_versions:
            if v["dimension_id"] == dim["id"] and v["version"] == version:
                vid = v
        if vid is None:
            return None
        before = dimension_core_defn(dim)
        restored = vid.get("after_json") or {}
        dim["name"] = restored.get("name") or dim["name"]
        dim["dim_type"] = restored.get("dim_type") or dim["dim_type"]
        dim["expr"] = restored.get("expr") or dim["expr"]
        dim["time_granularity"] = restored.get("time_granularity") or dim["time_granularity"]
        dim["description"] = restored.get("description") or ""
        dim["status"] = PUBLISHED
        dim["version"] = dim["version"] + 1
        dim["updated_at"] = datetime.now().isoformat()
        dim["approved_by"] = approved_by
        self._dimension_versions.append(
            {
                "dimension_id": dim["id"],
                "version": dim["version"],
                "change_type": "rollback",
                "before_json": before,
                "after_json": dimension_core_defn(dim),
                "operator": operator,
                "approved_by": approved_by,
                "created_at": dim["updated_at"],
            }
        )
        return dict(dim)

    async def list_dimension_versions(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        if dim is None:
            return []
        return [
            {
                "version": v["version"],
                "change_type": v["change_type"],
                "operator": v["operator"],
                "approved_by": v.get("approved_by", ""),
                "created_at": v.get("created_at"),
                "after_json": v.get("after_json"),
            }
            for v in self._dimension_versions
            if v["dimension_id"] == dim["id"]
        ]

    async def get_dimension_def(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        _model, dim = self._find_dimension(dimension_key, tenant_id)
        return dimension_core_defn(dim) if dim is not None else None


class InMemoryAuditLogRepository:
    """合规审计日志内存实现。"""

    def __init__(self) -> None:
        self._logs: List[Dict[str, Any]] = []
        self._next_id = 1

    async def add(self, event: Dict[str, Any]) -> int:
        row = dict(event)
        row["id"] = self._next_id
        self._next_id += 1
        row.setdefault("event_type", "unknown")
        row.setdefault("level", "info")
        row.setdefault("session_id", "")
        row.setdefault("user_id", "")
        row.setdefault("tenant_id", "")
        row.setdefault("round_id", "")
        row.setdefault("summary", "")
        row.setdefault("detail_json", event.get("details") or {})
        row.setdefault("client_info", "")
        row["created_at"] = datetime.now().isoformat()
        self._logs.append(row)
        return int(row["id"])

    async def query(
        self,
        *,
        event_type: Optional[str] = None,
        level: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows = [
            r
            for r in self._logs
            if (not event_type or r.get("event_type") == event_type)
            and (not level or r.get("level") == level)
            and (not session_id or r.get("session_id") == session_id)
            and (not user_id or r.get("user_id") == user_id)
        ]
        total = len(rows)
        return rows[offset : offset + limit], total

    async def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        by_level: Dict[str, int] = {}
        for r in self._logs:
            t = r.get("event_type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
            lv = r.get("level") or "info"
            by_level[lv] = by_level.get(lv, 0) + 1
        return {"by_type": by_type, "by_level": by_level}


class InMemoryErrorSampleRepository:
    """错误样本池内存实现（PRD FR6）。"""

    def __init__(self) -> None:
        self._samples: List[Dict[str, Any]] = []
        self._next_id = 1

    async def add(self, sample: Dict[str, Any]) -> int:
        row = dict(sample)
        row["id"] = self._next_id
        self._next_id += 1
        row.setdefault("session_id", "")
        row.setdefault("user_id", "")
        row.setdefault("tenant_id", "")
        row.setdefault("round_id", "")
        row.setdefault("error_code", "")
        row.setdefault("category", 0)
        row.setdefault("category_label", "")
        row.setdefault("node", "")
        row.setdefault("message", "")
        row.setdefault("detail", "")
        row.setdefault("retryable", False)
        row.setdefault("recovered", False)
        row.setdefault("fix_hint", "")
        row.setdefault("attempts", 1)
        row.setdefault("trace_snippet", "")
        row["created_at"] = datetime.now().isoformat()
        self._samples.append(row)
        return int(row["id"])

    async def query(
        self,
        *,
        category: Optional[int] = None,
        node: Optional[str] = None,
        recovered: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        rows = [
            r
            for r in self._samples
            if (category is None or r.get("category") == category)
            and (not node or r.get("node") == node)
            and (recovered is None or bool(r.get("recovered")) == recovered)
        ]
        total = len(rows)
        return rows[offset : offset + limit], total

    async def stats(self) -> Dict[str, Any]:
        by_category_label: Dict[str, int] = {}
        recovered = 0
        for r in self._samples:
            label = r.get("category_label") or "未知"
            by_category_label[label] = by_category_label.get(label, 0) + 1
            if r.get("recovered"):
                recovered += 1
        total = len(self._samples)
        return {
            "total": total,
            "recovered": recovered,
            "recovery_rate": round(recovered / total, 4) if total else 0.0,
            "by_category": by_category_label,
        }


class _GrantScopeMixin:
    """授权匹配公共逻辑：user_id 级 / role 级 / 全租户默认 三选一命中。"""

    @staticmethod
    def _match_scope(row, tenant_id: str, user_id: str, role: str) -> bool:
        if row.tenant_id and row.tenant_id != tenant_id:
            return False
        return (
            (row.user_id and row.user_id == user_id)
            or (row.role and row.role == role)
            or (not row.user_id and not row.role)
        )


class InMemoryMetricGrantRepository(_GrantScopeMixin):
    """指标授权内存实现（FR13 enforce 指标白名单）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[MetricGrantData] = []
        self._seq = 0

    async def add(self, grant: MetricGrantData) -> MetricGrantData:
        async with self._lock:
            self._seq += 1
            obj = MetricGrantData(
                id=self._seq,
                tenant_id=grant.tenant_id,
                user_id=grant.user_id,
                role=grant.role,
                metric_key=grant.metric_key,
                granted_by=grant.granted_by,
                created_at=_now(),
            )
            self._items.append(obj)
            return obj

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[MetricGrantData]:
        return [g for g in self._items if self._match_scope(g, tenant_id, user_id, role)]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[MetricGrantData]:
        rows = [g for g in self._items if not tenant_id or g.tenant_id == tenant_id]
        return rows[:limit]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, metric_key: str) -> bool:
        for idx, g in enumerate(self._items):
            if (
                (g.tenant_id or "") == (tenant_id or "")
                and (g.user_id or "") == (user_id or "")
                and (g.role or "") == (role or "")
                and g.metric_key == metric_key
            ):
                self._items.pop(idx)
                return True
        return False

    async def count(self) -> int:
        return len(self._items)


class InMemoryDataScopeRepository(_GrantScopeMixin):
    """行权限内存实现（FR13 enforce 行级过滤下推）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[DataScopeData] = []
        self._seq = 0

    async def add(self, scope: DataScopeData) -> DataScopeData:
        async with self._lock:
            self._seq += 1
            obj = DataScopeData(
                id=self._seq,
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                role=scope.role,
                dimension=scope.dimension,
                allowed_values=list(scope.allowed_values or []),
                granted_by=scope.granted_by,
                created_at=_now(),
            )
            self._items.append(obj)
            return obj

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[DataScopeData]:
        return [s for s in self._items if self._match_scope(s, tenant_id, user_id, role)]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[DataScopeData]:
        rows = [s for s in self._items if not tenant_id or s.tenant_id == tenant_id]
        return rows[:limit]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, dimension: str) -> bool:
        for idx, s in enumerate(self._items):
            if (
                (s.tenant_id or "") == (tenant_id or "")
                and (s.user_id or "") == (user_id or "")
                and (s.role or "") == (role or "")
                and s.dimension == dimension
            ):
                self._items.pop(idx)
                return True
        return False

    async def count(self) -> int:
        return len(self._items)


class InMemoryDimensionPolicyRepository(_GrantScopeMixin):
    """列/维度 deny 策略内存实现（FR13 enforce 列级硬剔除）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[DimensionPolicyData] = []
        self._seq = 0

    async def add(self, policy: DimensionPolicyData) -> DimensionPolicyData:
        async with self._lock:
            self._seq += 1
            obj = DimensionPolicyData(
                id=self._seq,
                tenant_id=policy.tenant_id,
                user_id=policy.user_id,
                role=policy.role,
                dimension=policy.dimension,
                denied=policy.denied,
                granted_by=policy.granted_by,
                created_at=_now(),
            )
            self._items.append(obj)
            return obj

    async def list_for(self, *, tenant_id: str, user_id: str, role: str) -> List[DimensionPolicyData]:
        return [p for p in self._items if self._match_scope(p, tenant_id, user_id, role)]

    async def list_all(self, *, tenant_id: str = "", limit: int = 500) -> List[DimensionPolicyData]:
        rows = [p for p in self._items if not tenant_id or p.tenant_id == tenant_id]
        return rows[:limit]

    async def delete(self, *, tenant_id: str, user_id: str, role: str, dimension: str) -> bool:
        for idx, p in enumerate(self._items):
            if (
                (p.tenant_id or "") == (tenant_id or "")
                and (p.user_id or "") == (user_id or "")
                and (p.role or "") == (role or "")
                and p.dimension == dimension
            ):
                self._items.pop(idx)
                return True
        return False

    async def count(self) -> int:
        return len(self._items)


class InMemorySemanticPublishedRepository:
    """语义层发布快照内存实现（混合方案：YAML 基座 → DB 发布 → 运行时装配）。

    - version 按租户内自增；
    - get_latest_published：优先该租户 published 且 version 最大者，无则回落全局；
    - mode 区分 full（整份快照）/ overlay（差异条目）。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: List[SemanticPublishedData] = []
        self._seq = 0

    async def add(self, rec: SemanticPublishedData) -> SemanticPublishedData:
        async with self._lock:
            self._seq += 1
            obj = SemanticPublishedData(
                id=self._seq,
                tenant_id=rec.tenant_id,
                version=rec.version,
                state=rec.state,
                mode=rec.mode or "full",
                payload=dict(rec.payload or {}),
                created_by=rec.created_by,
                created_at=_now(),
                published_at=_now() if rec.state == "published" else None,
                approved_by=rec.approved_by or "",
                rejected_by=rec.rejected_by or "",
                reject_reason=rec.reject_reason or "",
            )
            self._items.append(obj)
            return obj

    async def get(self, snapshot_id: int) -> Optional[SemanticPublishedData]:
        for r in self._items:
            if r.id == snapshot_id:
                return r
        return None

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
        async with self._lock:
            for r in self._items:
                if r.id == snapshot_id:
                    r.state = state
                    if state == "published":
                        if r.published_at is None:
                            r.published_at = _now()
                        if approved_by:
                            r.approved_by = approved_by
                        elif operator:
                            r.approved_by = operator
                    elif state == "draft" and (rejected_by or reject_reason):
                        if rejected_by:
                            r.rejected_by = rejected_by
                        if reject_reason:
                            r.reject_reason = reject_reason
                    return r
            return None

    async def get_latest_published(self, *, tenant_id: str) -> Optional[SemanticPublishedData]:
        cands = [
            r
            for r in self._items
            if r.state == "published" and (r.tenant_id == "" or r.tenant_id == tenant_id)
        ]
        if not cands:
            return None
        # 租户专属优先（非空 tenant_id），其次 version 大者，再次 id 大者
        cands.sort(key=lambda r: (1 if r.tenant_id else 0, r.version, r.id or 0), reverse=True)
        return cands[0]

    async def list_for(
        self, *, tenant_id: Optional[str] = None, limit: int = 100, visible_scope: bool = False
    ) -> List[SemanticPublishedData]:
        if visible_scope:
            if tenant_id is None:
                rows = list(self._items)
            else:
                tid = (tenant_id or "").strip()
                allowed = ("", tid) if tid else ("",)
                rows = [r for r in self._items if (r.tenant_id or "") in allowed]
        else:
            rows = [r for r in self._items if (r.tenant_id or "") == (tenant_id or "")]
        rows.sort(key=lambda r: (r.version, r.id or 0), reverse=True)
        return rows[:limit]

    async def count_for(self, *, tenant_id: str) -> int:
        return sum(1 for r in self._items if (r.tenant_id or "") == (tenant_id or ""))

    async def delete(self, *, tenant_id: str, version: int) -> bool:
        for idx, r in enumerate(self._items):
            if (r.tenant_id or "") == (tenant_id or "") and r.version == version:
                self._items.pop(idx)
                return True
        return False

    async def count(self) -> int:
        return len(self._items)
