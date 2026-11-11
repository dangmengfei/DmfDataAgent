"""仓储层协议（接口）。

上层（Agent 节点 / 服务层）只依赖这些 Protocol，
具体实现有 SQL（MySQL）与内存两种，由 db.factory 按配置装配，
保证 MySQL 不可用时框架仍可运行（降级模式）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Set

from dmf_agent.schemas.slang import SlangEntryData, SlangVersionData
from dmf_agent.schemas.knowledge import (
    ChunkData,
    DocumentData,
    KnowledgeBaseData,
)
from dmf_agent.schemas.trace import SpanData, TraceData
from dmf_agent.schemas.memory import DomainMemoryData, MemorySummaryData, UserPreferenceData
from dmf_agent.schemas.metrics import MetricData
from dmf_agent.schemas.session import (
    ConversationStateData,
    MessageData,
    SessionData,
)
from dmf_agent.schemas.feedback import BadCaseData, FewShotData
from dmf_agent.schemas.auth import RoleData, TenantData, UserData
from dmf_agent.schemas.permission import (
    DataScopeData,
    DimensionPolicyData,
    MetricGrantData,
)
from dmf_agent.schemas.semantic_publish import SemanticPublishedData


class SessionRepository(Protocol):
    async def create(
        self, user_id: str, title: Optional[str] = None, tenant_id: str = ""
    ) -> SessionData: ...

    async def get(self, session_id: str) -> Optional[SessionData]: ...

    async def list_by_user(self, user_id: str) -> List[SessionData]: ...

    async def list_by_tenant(self, tenant_id: str) -> List[SessionData]:
        """租户内会话列表（多租户隔离：跨租户不可见）。"""
        ...

    async def list_all(self, limit: int = 500) -> List[SessionData]:
        """全部会话（按更新时间倒序，上限 limit）。

        **管理面专用**（记忆管理页用 id→title 映射补会话标题，避免逐条查的 N+1）；
        业务链路一律走 list_by_user / list_by_tenant 以保证租户隔离。
        """
        ...

    async def rename(self, session_id: str, title: str) -> Optional[SessionData]: ...

    async def delete(self, session_id: str) -> bool: ...


class MessageRepository(Protocol):
    async def add(self, message: MessageData) -> MessageData: ...

    async def list_by_session(self, session_id: str) -> List[MessageData]: ...

    async def delete_by_session(self, session_id: str) -> int: ...

    async def delete_after(
        self, session_id: str, message_id: str, *, inclusive: bool = True
    ) -> int:
        """删除该会话中 ``message_id`` 及其之后（``inclusive=False`` 则不含自身）的全部消息，返回删除条数。

        供「编辑重发 → 替换该轮」使用：把被改写的那条用户消息（及其后续轮次）整段裁掉，
        再以新内容重发，避免追加式编辑把旧问答永久留在上下文里。排序口径与
        ``list_by_session`` 一致（created_at, id），保证删的是"同一段"。
        """
        ...

    async def get(self, message_id: str) -> Optional[MessageData]: ...


class DocumentRepository(Protocol):
    async def add(self, doc: DocumentData) -> DocumentData: ...

    async def get(self, doc_id: str) -> Optional[DocumentData]: ...

    async def list(
        self,
        keyword: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        authority_level: Optional[str] = None,
        source_uri: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> List[DocumentData]:
        """文档列表；`tenant_id` 非 None 时按租户过滤（含未归类文档）。

        未归类文档（knowledge_base_id 为空）同样带租户，因此不会因"没挂知识库"
        而对其他租户可见。

        `authority_level`（来源分级）与 `source_uri`（权威源灌入的幂等键）为 FR7
        新增的过滤维度，None 表示不过滤。`owner_id` 非 None 时只返回归属该用户的文档
        （检索隔离：个人/私有库互不可见），None 表示不过滤。
        """
        ...

    async def count_documents(self, knowledge_base_id: str) -> int: ...

    async def count_by_authority(self) -> Dict[str, int]:
        """按来源分级统计文档数：{authoritative|sediment|user: count}。"""
        ...

    async def update_status(
        self, doc_id: str, status: str, chunk_count: Optional[int] = None
    ) -> Optional[DocumentData]: ...

    async def delete(self, doc_id: str) -> bool: ...


class ChunkRepository(Protocol):
    async def add_many(self, chunks: List[ChunkData]) -> List[ChunkData]: ...

    async def list_by_document(self, doc_id: str) -> List[ChunkData]: ...

    async def list_all(self) -> List[ChunkData]:
        """全部分块（BM25 混合检索的语料来源）。"""
        ...

    async def delete_by_document(self, doc_id: str) -> int: ...


class KnowledgeBaseRepository(Protocol):
    """知识库（个人 / 企业）仓储。"""

    async def add(self, base: KnowledgeBaseData) -> KnowledgeBaseData: ...

    async def get(self, base_id: str) -> Optional[KnowledgeBaseData]: ...

    async def list(
        self,
        type: Optional[str] = None,
        keyword: Optional[str] = None,
        owner_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[KnowledgeBaseData]:
        """知识库列表；`tenant_id` 非 None 时按租户过滤。"""
        ...

    async def delete(self, base_id: str) -> bool: ...


class MemorySummaryRepository(Protocol):
    """会话滚动摘要仓储（对话记忆压缩，L1 会话记忆）。

    L1 双写：MySQL 主存 + Redis 热层（TTL）。Redis 不可用时仅依赖 MySQL。
    """

    async def get(self, session_id: str) -> Optional[MemorySummaryData]: ...

    async def upsert(self, summary: MemorySummaryData) -> MemorySummaryData: ...

    async def delete(self, session_id: str) -> None:
        """清会话（删除 L1 摘要）；热层与 MySQL 同步清理。"""
        ...

    async def list_recent(self, limit: int = 100, keyword: str = "") -> List[MemorySummaryData]:
        """摘要列表（管理面：跨会话浏览"都有哪些会话记忆"）。

        keyword 命中摘要正文（大小写不敏感），空串表示不过滤。
        """
        ...

    async def count(self) -> int:
        """摘要总条数（管理页统计卡）。"""
        ...


class ConversationStateRepository(Protocol):
    """结构化对话状态仓储（P0-6 多轮对话状态管理）。

    每会话一行（session_id 主键），保存最近一轮已解析实体与状态机状态；
    下一轮由 ChatService 读取注入 `context_entities`。
    """

    async def get(self, session_id: str) -> Optional["ConversationStateData"]: ...

    async def upsert(self, state: "ConversationStateData") -> "ConversationStateData": ...

    async def delete(self, session_id: str) -> bool: ...

    async def list_recent(
        self, limit: int = 100, tenant_id: Optional[str] = None
    ) -> List["ConversationStateData"]:
        """最近更新的状态列表（管理面浏览；tenant_id 非 None 时按租户过滤）。"""
        ...


class MetricRepository(Protocol):
    """指标元数据仓储（迁移自老系统 metadata/hm_meta 指标定义）。"""

    async def find_by_name_or_code(self, name_or_code: str) -> Optional[MetricData]: ...

    async def search(self, keyword: Optional[str] = None, limit: int = 20) -> List[MetricData]: ...

    async def list_all(self) -> List[MetricData]: ...

    async def upsert_many(self, metrics: List[MetricData]) -> int: ...



class SlangRepository(Protocol):
    """黑话词典仓储（三级：行业级 / 公司级 / 用户级）。

    解析链路只调 `list_layers`（一次取齐三层已发布词条，供 Redis 缓存）；
    管理面调 query / get / upsert / update / delete / count_by_scope / facets。
    """

    async def list_layers(
        self, tenant_id: str = "", user_id: str = ""
    ) -> Dict[str, List[SlangEntryData]]:
        """取三层**已发布**词条（返回 {"industry": [...], "tenant": [...], "user": [...]}）。

        缺失的层级返回空列表，键始终齐全，便于上层按固定顺序覆盖。
        """
        ...

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
        """条件过滤 + 分页；返回 (词条, 命中总数)。管理面列表用。"""
        ...

    async def get(self, entry_id: int) -> Optional[SlangEntryData]: ...

    async def find(self, entry: SlangEntryData) -> Optional[SlangEntryData]:
        """按 (scope, tenant_id, user_id, entry_type, term) 唯一定位（导入去重用）。"""
        ...

    async def add(self, entry: SlangEntryData) -> SlangEntryData: ...

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
        """部分更新（None 表示不改）；每次更新 version+1 并写版本审计。"""
        ...

    async def delete(self, entry_id: int, operator: str = "") -> bool: ...

    async def count_by_scope(self) -> Dict[str, int]:
        """按层级计数（管理页统计卡）。"""
        ...

    async def count(self, status: Optional[str] = None) -> int: ...

    async def add_version(self, version: SlangVersionData) -> SlangVersionData: ...

    async def list_versions(self, entry_id: int) -> List[SlangVersionData]: ...

    async def bump_hit(self, entry_ids: List[int]) -> int:
        """命中计数累加（改写节点异步回写，用于管理页观察词条热度）。"""
        ...

    async def replace_scope_entries(
        self, scope: str, tenant_id: str, user_id: str, entries: List[SlangEntryData]
    ) -> int:
        """用给定集合整体替换某层（「重置为内置种子」用）。返回写入条数。"""
        ...

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
        """改写节点埋点：每次改写 best-effort 落一条，返回事件 id。"""
        ...

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
        """回写误改写反馈：按 message_id（优先）→ query_hash 命中事件置 feedback；
        无则新建。`cancel=True` 撤销上一次反馈（feedback 置 NULL），忽略 is_correct。返回事件 id。"""
        ...

    async def get_rewrite_feedback_by_messages(
        self, message_ids: List[str]
    ) -> Dict[str, int]:
        """按 message_id 批量取改写反馈值（1=正确/-1=误改写），供历史消息回显；仅含已反馈。"""
        ...

    async def query_rewrite_events(
        self, since: Optional[datetime] = None, limit: int = 10000
    ) -> List[Dict[str, Any]]:
        """取改写事件（命中率/误改写率聚合用）。"""
        ...

    async def count_rewrite_events(self, since: Optional[datetime] = None) -> int: ...


class TraceRepository(Protocol):
    """对话观测 trace 仓储（每轮一条；前端观测面板数据源）。"""

    async def add(self, record: TraceData) -> TraceData: ...

    async def list_recent(self, limit: int = 50) -> List[TraceData]: ...

    async def get(self, session_id: str, round_id: str) -> Optional[TraceData]: ...

    async def aggregate(self, limit: int = 500) -> Dict[str, Any]: ...

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
        """条件过滤 + 分页；返回 (记录, 命中总数)。观测专页明细列表用。"""
        ...

    async def aggregate_filtered(
        self,
        *,
        session_id: Optional[str] = None,
        intent: Optional[str] = None,
        has_error: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """带同样过滤条件的汇总（与 query 口径一致）。"""
        ...

    async def cost_breakdown(
        self,
        *,
        group_by: str = "model",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """FR11 成本归集：按 model / user / tenant / day / intent 分组汇总。

        每组返回 `{key, rounds, prompt_tokens, completion_tokens, total_tokens, cost}`，
        按 cost 降序；`limit` 只截断分组数（不截断参与聚合的行数）。
        """
        ...


class SpanRepository(Protocol):
    """OpenTelemetry span 仓储（`trace_spans` 表；观测专页明细 / 导出数据源）。"""

    async def add_many(self, spans: List[SpanData]) -> int: ...

    async def list_by_round(self, session_id: str, round_id: str) -> List[SpanData]: ...

    async def list_by_session(
        self, session_id: str, limit: int = 500
    ) -> List[SpanData]: ...

    async def list_recent(self, limit: int = 200) -> List[SpanData]: ...

    async def aggregate(self, limit: int = 2000) -> Dict[str, Any]: ...


class FewShotRepository(Protocol):
    """few-shot 样本库（意图识别阶段 BM25 召回；管理面 CRUD）。

    两种来源共用本表（source=like 点赞沉淀 / manual 管理面录入），
    召回与列表口径一致 —— 「页面上看到的样本 = 真正会被召回候选的样本」。
    """

    async def add(self, example: FewShotData) -> FewShotData: ...

    async def list_all(self, status: Optional[str] = None) -> List[FewShotData]:
        """全部样本（按创建时间倒序）；`status` 非 None 时按其过滤（召回传 'active'）。"""
        ...

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
        """条件过滤 + 分页；返回 (样本, 命中总数)。管理面列表用。"""
        ...

    async def get_by_message(self, message_id: str) -> Optional[FewShotData]: ...

    async def delete_by_message(self, message_id: str) -> bool: ...

    async def get_by_id(self, item_id: int) -> Optional[FewShotData]: ...

    async def find_by_question(
        self, question: str, category: Optional[str] = None
    ) -> Optional[FewShotData]:
        """按问题原文精确匹配（导入去重用）。"""
        ...

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
        """部分更新（None 表示不改）；样本不存在返回 None。"""
        ...

    async def delete(self, item_id: int) -> bool: ...

    async def count(self, status: Optional[str] = None, source: Optional[str] = None) -> int:
        """样本条数（管理页统计卡：总量 / 启用数 / 点赞数 / 手工数）。"""
        ...


class BadCaseRepository(Protocol):
    """点踩样本库（bad case；当前仅落库，供后续分析 / 回流）。"""

    async def add(self, case: BadCaseData) -> BadCaseData: ...

    async def list_recent(self, limit: int = 50) -> List[BadCaseData]: ...

    async def get_by_message(self, message_id: str) -> Optional[BadCaseData]: ...

    async def delete_by_message(self, message_id: str) -> bool: ...

    async def count(self) -> int:
        """点踩总数（FR11 采纳率分母：与点赞数共同构成反馈总量）。"""
        ...


class DomainMemoryRepository(Protocol):
    """领域知识记忆（L3，团队 / 域级沉淀，审核制）。

    few_shot_examples 视为本表 type='few_shot' & status='approved' 的「已验证样本」视图；
    意图识别阶段合并二者做 BM25 召回。写入以 add 为主（点赞沉淀 / 管理员录入），
    list_approved 供召回消费，query / update / delete 供管理端审核与清理。
    """

    async def add(self, item: DomainMemoryData) -> DomainMemoryData: ...

    async def list_all(self) -> List[DomainMemoryData]: ...

    async def list_approved(
        self, domain: Optional[str] = None, type: Optional[str] = None
    ) -> List[DomainMemoryData]: ...

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
        """条件过滤 + 分页；返回 (记忆, 命中总数)。管理面列表用。

        与 list_approved（召回专用、按权重排序）的区别：本方法不过滤状态、
        按创建时间倒序，并把 keyword 同时匹配 question / answer / tags。
        """
        ...

    async def get_by_id(self, item_id: int) -> Optional[DomainMemoryData]: ...

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
        """部分更新（None 表示不改；审核即改 status）；不存在返回 None。"""
        ...

    async def delete(self, item_id: int) -> None: ...

    async def count_by_type(self) -> Dict[str, int]:
        """按 type 计数（管理页统计卡：rule / glossary / correction / few_shot 各多少）。"""
        ...


class UserPreferenceRepository(Protocol):
    """用户偏好仓储（L2，个人维度长期记忆）。

    主键维度 (user_id, key)：`upsert` 覆盖同键记录，避免同一偏好堆积多条。
    """

    async def upsert(self, pref: UserPreferenceData) -> UserPreferenceData: ...

    async def list_by_user(
        self, user_id: str, status: Optional[str] = None
    ) -> List[UserPreferenceData]: ...

    async def list_all(
        self,
        *,
        tenant_id: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[UserPreferenceData]:
        """全平台偏好（管理面 L2 视角，按更新时间倒序）。

        与 list_by_user（本人视角）的区别：本方法可跨用户查询并支持租户/来源过滤，
        路由层需以 `memory` 权限点守卫（否则会越权读到他人偏好）。
        """
        ...

    async def get(self, user_id: str, key: str) -> Optional[UserPreferenceData]: ...

    async def delete(self, user_id: str, key: str) -> bool: ...

    async def delete_by_id(self, pref_id: int) -> bool:
        """按主键删除（管理面跨用户删除；本人接口仍用 delete(user_id, key)）。"""
        ...

    async def clear_user(self, user_id: str) -> int: ...

    async def count(self) -> int:
        """偏好总条数（管理页统计卡）。"""
        ...


class TenantRepository(Protocol):
    """租户仓储（多租户隔离最上层）。"""

    async def add(self, tenant: TenantData) -> TenantData: ...

    async def get(self, tenant_id: str) -> Optional[TenantData]: ...

    async def get_by_code(self, code: str) -> Optional[TenantData]: ...

    async def list_all(self) -> List[TenantData]: ...

    async def update(
        self, tenant_id: str, name: Optional[str] = None, status: Optional[str] = None
    ) -> Optional[TenantData]: ...

    async def delete(self, tenant_id: str) -> bool: ...


class UserRepository(Protocol):
    """用户仓储（登录主体；哈希口令，不存明文）。"""

    async def add(self, user: UserData, password_hash: str) -> UserData: ...

    async def get(self, user_id: str) -> Optional[UserData]: ...

    async def get_by_username(self, username: str) -> Optional[UserData]: ...

    async def get_password_hash(self, user_id: str) -> Optional[str]: ...

    async def list_all(self, tenant_id: Optional[str] = None) -> List[UserData]: ...

    async def update(
        self,
        user_id: str,
        display_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[UserData]: ...

    async def set_password(self, user_id: str, password_hash: str) -> bool: ...

    async def touch_login(self, user_id: str) -> None: ...

    async def delete(self, user_id: str) -> bool: ...

    async def count(self) -> int: ...


class RoleRepository(Protocol):
    """角色仓储（RBAC 角色与权限点；内置三角色由种子导入）。"""

    async def add(self, role: RoleData) -> RoleData: ...

    async def get(self, name: str) -> Optional[RoleData]: ...

    async def list_all(self) -> List[RoleData]: ...

    async def update(
        self,
        name: str,
        *,
        label: Optional[str] = None,
        description: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        status: Optional[str] = None,
    ) -> Optional[RoleData]: ...

    async def delete(self, name: str) -> bool: ...

    async def count(self) -> int: ...


class SemanticRegistryRepository(Protocol):
    """语义注册中心仓储（只读 + 种子导入 + 版本与口径审批）。

    租户口径（**读得到 ≠ 改得动**，两套语义刻意分开）
    ------------------------------------------------
    - **可见性**（列表 / 详情 / 统计 / facets）：``tenant_id=None`` 不过滤（系统全量视角）；
      ``""`` 仅全局条目；``"t"`` = 全局 ∪ 本租户（全局对租户**只读**）。
    - **归属**（写 / 按 key 精确定位）：``tenant_id=None`` 为历史行为（任意行）；
      否则要求**恰好**属于该租户 —— 于是"租户改到了全局定义"在仓储层就是定位失败
      （上层 404），而不是指望每个调用点都记得校验。
    - ``semantic_models`` / ``semantic_metrics`` 自带 ``tenant_id``；
      ``semantic_dimensions`` / ``semantic_measures`` 无该列，租户归属**继承宿主模型**。

    一个例外：``list_all_metric_defs`` / ``list_all_measure_keys`` 是**引用校验的取值域**，
    必须走可见性（全局 ∪ 本租户）而非归属 —— 否则租户指标引用全局 measure 会被判成
    "引用不存在"。
    """

    async def list_models(
        self,
        *,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """语义模型列表（含维度 / 度量计数）；tenant_id 按可见性收敛。"""
        ...

    async def get_model(
        self, model_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """语义模型详情（含 entities / dimensions / measures 全量）。"""
        ...

    async def list_dimensions(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """维度列表（可按模型过滤）；租户归属经宿主模型收敛。"""
        ...

    async def list_measures(
        self,
        *,
        model_key: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        tenant_id: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """度量列表（可按模型过滤）；租户归属经宿主模型收敛。"""
        ...

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
        """指标列表（keyword 命中 name / metric_key / aliases）；按可见性收敛。"""
        ...

    async def get_metric(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """指标详情（含宿主模型与可用维度）；tenant_id 按可见性收敛。"""
        ...

    async def stats(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """统计卡（模型 / 指标 / 维度 / 度量数 + 状态 / 类型分布）；按可见性收敛。"""
        ...

    async def facets(self, *, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """过滤选项（类型 / 状态 / 模型 key / 单位）；按可见性收敛。"""
        ...

    async def import_seed(self, seed: Dict[str, Any], *, operator: str = "system") -> Dict[str, Any]:
        """幂等导入种子：按 (scope=industry, key) 去重，已存在跳过。

        返回 {"models_imported": n, "metrics_imported": n,
              "models_skipped": n, "metrics_skipped": n}。
        """
        ...

    async def count_models(self) -> int:
        """模型总数（启动时判断是否需要灌种子）。"""
        ...

    # ---------- 写路径（P2：版本与口径审批）----------

    async def create_metric_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新建指标草稿（status=draft, version=1），返回创建后的 brief。

        重复 metric_key 自动追加后缀（唯一性是 (scope, tenant_id, key)，
        所以同 key 在别租户下可以存在 —— 这正是"租户覆盖全局"的机制）；
        口径校验在 publish 阶段执行。``tenant_id`` 非 None 时**强制覆盖**载荷里的归属。
        """
        ...

    async def update_metric(
        self,
        metric_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """编辑指标（仅 draft/review 可编辑；published/deprecated 需先废弃）。

        返回更新后的 brief；不存在返回 None；非法状态抛 ValueError。
        ``tenant_id`` 非 None 时按**归属**定位（改不到全局条目）。
        """
        ...

    async def submit_metric_review(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """草稿提交审核（draft -> review）；按归属定位。"""
        ...

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
        """审批发布（draft/review -> published）；按归属定位。

        返回 {"ok", "metric"?, "validation"?, "error"?}：
        - ok=False, error="not_found"       指标不存在
        - ok=False, error="illegal_transition" 当前状态不可发布
        - ok=False, validation=<结果>       六道校验未通过
        - ok=True,  metric=<brief>          发布成功（version 自增）
        """
        ...

    async def deprecate_metric(
        self,
        metric_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """废弃（published/review -> deprecated，version 自增）；按归属定位。"""
        ...

    async def rollback_metric(
        self,
        metric_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """回滚到指定版本（产生新版本，status=published）；按归属定位。"""
        ...

    async def list_metric_versions(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """版本快照列表（按 version 升序）；按归属定位。"""
        ...

    async def get_metric_version(
        self, metric_key: str, version: int, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """单条版本快照；按归属定位。"""
        ...

    async def list_all_metric_defs(
        self, *, tenant_id: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """**引用校验的取值域**：全局 ∪ 本租户（可见性，不是归属）；全局在前、租户在后
        （同名条目由租户覆盖）。key -> {metric_type, type_params, status, tenant_id}。"""
        ...

    async def list_all_measure_keys(
        self, *, tenant_id: Optional[str] = None
    ) -> Set[str]:
        """引用校验取值域 = 全局 ∪ 本租户的度量 key 集合（经宿主模型收敛）。"""
        ...

    async def get_metric_def(
        self, metric_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """单指标核心定义（口径），供校验；按归属定位。"""
        ...

    # 维度（与指标对称）

    async def create_dimension_draft(
        self,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新建维度草稿（status=draft, version=1）。

        维度租户归属继承宿主模型；``tenant_id`` 非 None 时要求宿主模型**恰属**该租户，
        否则租户就能往全局模型上挂自己的维度。
        """
        ...

    async def update_dimension(
        self,
        dimension_key: str,
        defn: Dict[str, Any],
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """编辑维度（仅 draft/review 可编辑）；按宿主模型归属定位。"""
        ...

    async def submit_dimension_review(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """维度提交审核；按宿主模型归属定位。"""
        ...

    async def publish_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """维度审批发布（schema 校验通过后 draft/review -> published）；按归属定位。"""
        ...

    async def deprecate_dimension(
        self,
        dimension_key: str,
        *,
        operator: str = "system",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """维度废弃；按宿主模型归属定位。"""
        ...

    async def rollback_dimension(
        self,
        dimension_key: str,
        version: int,
        *,
        operator: str = "system",
        approved_by: str = "",
        tenant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """维度回滚到指定版本；按宿主模型归属定位。"""
        ...

    async def list_dimension_versions(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """维度版本快照列表；按宿主模型归属定位。"""
        ...

    async def get_dimension_def(
        self, dimension_key: str, *, tenant_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """单维度核心定义；按宿主模型归属定位。"""
        ...


class AuditLogRepository(Protocol):
    """合规审计日志仓储（PRD FR13：全链路审计）。"""

    async def add(self, event: Dict[str, Any]) -> int:
        """写入一条审计事件，返回事件 id。"""
        ...

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
        """审计日志列表 + 总数。"""
        ...

    async def stats(self) -> Dict[str, Any]:
        """按 event_type / level 统计。"""
        ...


class ErrorSampleRepository(Protocol):
    """错误样本池仓储（PRD FR6：报错分类 + 限次自愈样本落库）。"""

    async def add(self, sample: Dict[str, Any]) -> int:
        """写入一条错误样本，返回样本 id。"""
        ...

    async def query(
        self,
        *,
        category: Optional[int] = None,
        node: Optional[str] = None,
        recovered: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """错误样本列表 + 总数（可按分类 / 节点 / 是否自愈过滤）。"""
        ...

    async def stats(self) -> Dict[str, Any]:
        """按 category / node / recovered 统计（自愈成功率等）。"""
        ...


class MetricGrantRepository(Protocol):
    """指标授权仓储（PRD FR13 P5/P6 enforce 指标白名单）。

    匹配规则（list_for）：主体命中的授权 = user_id 级（grant.user_id==user_id）
    或 role 级（grant.role==role）或全租户默认（user_id='' 且 role=''）的并集。
    """

    async def add(self, grant: MetricGrantData) -> MetricGrantData: ...

    async def list_for(
        self, *, tenant_id: str, user_id: str, role: str
    ) -> List[MetricGrantData]:
        """返回适用于该主体的全部指标授权。"""
        ...

    async def list_all(
        self, *, tenant_id: str = "", limit: int = 500
    ) -> List[MetricGrantData]: ...

    async def delete(
        self, *, tenant_id: str, user_id: str, role: str, metric_key: str
    ) -> bool:
        """撤销一条授权（按主体 + metric_key 精确匹配；user_id/role 都为空则删全租户默认）。"""
        ...

    async def count(self) -> int: ...


class DataScopeRepository(Protocol):
    """行权限仓储（PRD FR13 P6 enforce 行级过滤下推）。"""

    async def add(self, scope: DataScopeData) -> DataScopeData: ...

    async def list_for(
        self, *, tenant_id: str, user_id: str, role: str
    ) -> List[DataScopeData]: ...

    async def list_all(
        self, *, tenant_id: str = "", limit: int = 500
    ) -> List[DataScopeData]: ...

    async def delete(
        self, *, tenant_id: str, user_id: str, role: str, dimension: str
    ) -> bool: ...

    async def count(self) -> int: ...


class DimensionPolicyRepository(Protocol):
    """列/维度 deny 策略仓储（PRD FR13 P6 enforce 列级硬剔除）。"""

    async def add(self, policy: DimensionPolicyData) -> DimensionPolicyData: ...

    async def list_for(
        self, *, tenant_id: str, user_id: str, role: str
    ) -> List[DimensionPolicyData]: ...

    async def list_all(
        self, *, tenant_id: str = "", limit: int = 500
    ) -> List[DimensionPolicyData]: ...

    async def delete(
        self, *, tenant_id: str, user_id: str, role: str, dimension: str
    ) -> bool: ...

    async def count(self) -> int: ...


class SemanticPublishedRepository(Protocol):
    """语义层发布快照仓储（混合方案：YAML 基座 → DB 发布 → 运行时装配）。

    回落语义：`get_latest_published(tenant_id=X)` 先取该租户最新 published，
    没有再回落全局（`tenant_id=""`），都没有返回 None。

    `mode` 区分 full（整份快照）/ overlay（只含差异条目）；
    链式解析（把 overlay 叠加到最近的 full 之上）在语义层完成，不在仓储里做。
    """

    async def add(self, rec: SemanticPublishedData) -> SemanticPublishedData: ...

    async def get(self, snapshot_id: int) -> Optional[SemanticPublishedData]:
        """按主键取单条快照（审批流用）。"""
        ...

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
        """推进/回退快照状态机；不存在返回 None。

        state 置为 ``published`` 时由实现补写 ``published_at`` 并记录 ``approved_by``
        （缺省回落到 ``operator``）；置为 ``draft`` 且带 ``rejected_by``/``reject_reason``
        时记录驳回留痕（T198d 审批审计）。
        """
        ...

    async def get_latest_published(
        self, *, tenant_id: str
    ) -> Optional[SemanticPublishedData]:
        """该租户最新 published 快照；无则回落全局；仍无返回 None。"""
        ...

    async def list_for(
        self, *, tenant_id: Optional[str] = None, limit: int = 100, visible_scope: bool = False
    ) -> List[SemanticPublishedData]:
        """列出快照版本（管理面/审计用）。

        ``tenant_id=None`` + ``visible_scope=False`` 等价于旧 ``tenant_id=""``（仅全局）；
        ``visible_scope=True`` 时按读作用域过滤（None=全量 / 空串=仅全局 / 具体 id=全局+该租户）。
        """
        ...

    async def count_for(self, *, tenant_id: str) -> int:
        """该租户已有快照数（用于分配下一个 version）。"""
        ...

    async def delete(self, *, tenant_id: str, version: int) -> bool: ...

    async def count(self) -> int: ...
