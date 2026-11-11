"""数据库装配工厂。

按配置装配 MySQL 或内存实现；MySQL 连接失败时自动降级内存模式，
保证框架在任何环境下都能启动（降级信息可通过 /api/v1/health 查看）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from dmf_agent.config.settings import Settings
from dmf_agent.db import models
from dmf_agent.db.redis_client import build_redis_client
from dmf_agent.db.base import (
    AuditLogRepository,
    BadCaseRepository,
    ConversationStateRepository,
    ChunkRepository,
    DocumentRepository,
    DomainMemoryRepository,
    ErrorSampleRepository,
    FewShotRepository,
    KnowledgeBaseRepository,
    MemorySummaryRepository,
    MessageRepository,
    MetricRepository,
    SemanticRegistryRepository,
    RoleRepository,
    SessionRepository,
    SlangRepository,
    SpanRepository,
    TenantRepository,
    TraceRepository,
    UserPreferenceRepository,
    UserRepository,
    MetricGrantRepository,
    DataScopeRepository,
    DimensionPolicyRepository,
    SemanticPublishedRepository,
)
from dmf_agent.db.memory import (
    InMemoryAuditLogRepository,
    InMemoryBadCaseRepository,
    InMemoryConversationStateRepository,
    InMemoryErrorSampleRepository,
    InMemorySemanticRegistryRepository,
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryDomainMemoryRepository,
    InMemoryFewShotRepository,
    InMemoryKnowledgeBaseRepository,
    InMemoryMemorySummaryRepository,
    InMemoryMessageRepository,
    InMemoryMetricRepository,
    InMemorySessionRepository,
    InMemorySlangRepository,
    InMemorySpanRepository,
    InMemoryRoleRepository,
    InMemoryTenantRepository,
    InMemoryTraceRepository,
    InMemoryUserPreferenceRepository,
    InMemoryUserRepository,
    InMemoryMetricGrantRepository,
    InMemoryDataScopeRepository,
    InMemoryDimensionPolicyRepository,
    InMemorySemanticPublishedRepository,
)
from dmf_agent.db.repositories import (
    SqlAuditLogRepository,
    SqlBadCaseRepository,
    SqlErrorSampleRepository,
    SqlSemanticRegistryRepository,
    SqlConversationStateRepository,
    SqlChunkRepository,
    SqlDocumentRepository,
    SqlDomainMemoryRepository,
    SqlFewShotRepository,
    SqlKnowledgeBaseRepository,
    SqlMemorySummaryRepository,
    SqlMessageRepository,
    SqlMetricRepository,
    SqlSessionRepository,
    SqlSlangRepository,
    SqlSpanRepository,
    SqlRoleRepository,
    SqlTenantRepository,
    SqlTraceRepository,
    SqlUserPreferenceRepository,
    SqlUserRepository,
    SqlMetricGrantRepository,
    SqlDataScopeRepository,
    SqlDimensionPolicyRepository,
    SqlSemanticPublishedRepository,
)
from dmf_agent.db.stores import LLMConfigStore, PluginStore, PromptVersionStore

logger = logging.getLogger(__name__)


@dataclass
class DatabaseBundle:
    mode: str  # "mysql" | "memory"
    engine: Optional[AsyncEngine] = None
    session_repo: SessionRepository = field(default_factory=InMemorySessionRepository)
    message_repo: MessageRepository = field(default_factory=InMemoryMessageRepository)
    document_repo: DocumentRepository = field(default_factory=InMemoryDocumentRepository)
    chunk_repo: ChunkRepository = field(default_factory=InMemoryChunkRepository)
    knowledge_base_repo: KnowledgeBaseRepository = field(
        default_factory=InMemoryKnowledgeBaseRepository
    )
    metric_repo: MetricRepository = field(default_factory=InMemoryMetricRepository)
    memory_repo: MemorySummaryRepository = field(default_factory=InMemoryMemorySummaryRepository)
    # 黑话词典（三级：行业级 / 公司级 / 用户级；查询改写的数据源）
    slang_repo: SlangRepository = field(default_factory=InMemorySlangRepository)
    # 语义注册中心（P1 骨架：模型/维度/度量/指标的注册镜像 + 种子导入）
    semantic_registry_repo: SemanticRegistryRepository = field(
        default_factory=InMemorySemanticRegistryRepository
    )
    trace_repo: TraceRepository = field(default_factory=InMemoryTraceRepository)
    span_repo: SpanRepository = field(default_factory=InMemorySpanRepository)
    # 点赞样本库（few-shot 经验库数据源；意图识别阶段 BM25 召回）
    few_shot_repo: FewShotRepository = field(default_factory=InMemoryFewShotRepository)
    # 点踩样本库（bad case；当前仅落库，供后续分析 / 回流）
    bad_case_repo: BadCaseRepository = field(default_factory=InMemoryBadCaseRepository)
    # 领域知识记忆（L3，团队 / 域级沉淀；意图识别阶段与 few-shot 合并召回）
    domain_memory_repo: DomainMemoryRepository = field(default_factory=InMemoryDomainMemoryRepository)
    # 租户 / 用户（认证与多租户隔离）
    tenant_repo: TenantRepository = field(default_factory=InMemoryTenantRepository)
    user_repo: UserRepository = field(default_factory=InMemoryUserRepository)
    role_repo: RoleRepository = field(default_factory=InMemoryRoleRepository)
    # L2 用户偏好（个人维度长期记忆；对话时注入 prompt，可由用户/系统沉淀）
    preference_repo: UserPreferenceRepository = field(
        default_factory=InMemoryUserPreferenceRepository
    )
    # 合规审计日志（PRD FR13）
    audit_log_repo: AuditLogRepository = field(default_factory=InMemoryAuditLogRepository)
    # 错误样本池（PRD FR6：报错分类 + 限次自愈样本落库）
    error_sample_repo: ErrorSampleRepository = field(default_factory=InMemoryErrorSampleRepository)
    # 结构化对话状态（P0-6 多轮对话状态管理：跨轮实体携带 + 确定性指代消解）
    conversation_state_repo: ConversationStateRepository = field(
        default_factory=InMemoryConversationStateRepository
    )
    # 管理端存储（插件注册 / 提示词版本）；非对话主链路，独立双态实现
    plugin_store: PluginStore = field(default_factory=PluginStore)
    prompt_store: PromptVersionStore = field(default_factory=PromptVersionStore)
    # 对话 LLM 模型配置（模型管理）；双态实现，is_default 条目作为运行时生效模型
    llm_config_store: LLMConfigStore = field(default_factory=LLMConfigStore)
    # 授权仓储（PRD FR13 P5/P6 enforce：指标白名单 / 行权限 / 列 deny）
    metric_grant_repo: MetricGrantRepository = field(default_factory=InMemoryMetricGrantRepository)
    data_scope_repo: DataScopeRepository = field(default_factory=InMemoryDataScopeRepository)
    dimension_policy_repo: DimensionPolicyRepository = field(
        default_factory=InMemoryDimensionPolicyRepository
    )
    # 语义层发布快照（混合方案：YAML 基座 → DB 发布 → 运行时装配）
    semantic_published_repo: SemanticPublishedRepository = field(
        default_factory=InMemorySemanticPublishedRepository
    )
    # Redis 热层客户端（记忆分层 L1 / 热点缓存）；None 表示未启用，仓储自动降级 MySQL/内存
    redis_client: Optional[Any] = None

    async def initialize(self, settings: Settings) -> None:
        """连接校验 + 建表；失败则整体降级为内存模式。"""
        if self.mode != "mysql" or self.engine is None:
            return
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if settings.db.auto_create_tables:
                async with self.engine.begin() as conn:
                    await conn.run_sync(models.Base.metadata.create_all)
            logger.info("MySQL 已连接：%s@%s:%s/%s",
                        settings.db.user, settings.db.host,
                        settings.db.port, settings.db.database)
        except Exception as exc:  # noqa: BLE001 - 任何连接错误都降级
            logger.warning("MySQL 连接失败，降级为内存存储（数据不持久化）：%s", exc)
            self._fallback_to_memory()

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    def _fallback_to_memory(self) -> None:
        self.mode = "memory"
        self.engine = None
        self.session_repo = InMemorySessionRepository()
        self.message_repo = InMemoryMessageRepository()
        self.document_repo = InMemoryDocumentRepository()
        self.chunk_repo = InMemoryChunkRepository()
        self.knowledge_base_repo = InMemoryKnowledgeBaseRepository()
        self.metric_repo = InMemoryMetricRepository()
        self.memory_repo = InMemoryMemorySummaryRepository()
        self.slang_repo = InMemorySlangRepository()
        self.semantic_registry_repo = InMemorySemanticRegistryRepository()
        self.trace_repo = InMemoryTraceRepository()
        self.span_repo = InMemorySpanRepository()
        self.few_shot_repo = InMemoryFewShotRepository()
        self.bad_case_repo = InMemoryBadCaseRepository()
        self.domain_memory_repo = InMemoryDomainMemoryRepository()
        self.tenant_repo = InMemoryTenantRepository()
        self.user_repo = InMemoryUserRepository()
        self.role_repo = InMemoryRoleRepository()
        self.preference_repo = InMemoryUserPreferenceRepository()
        self.audit_log_repo = InMemoryAuditLogRepository()
        self.error_sample_repo = InMemoryErrorSampleRepository()
        self.conversation_state_repo = InMemoryConversationStateRepository()
        self.plugin_store = PluginStore()
        self.prompt_store = PromptVersionStore()
        self.llm_config_store = LLMConfigStore()
        self.metric_grant_repo = InMemoryMetricGrantRepository()
        self.data_scope_repo = InMemoryDataScopeRepository()
        self.dimension_policy_repo = InMemoryDimensionPolicyRepository()
        self.semantic_published_repo = InMemorySemanticPublishedRepository()


def build_database(settings: Settings) -> DatabaseBundle:
    if not settings.db.enabled:
        logger.info("MySQL 未启用（DMF_DB-ENABLED=false），使用内存存储")
        return DatabaseBundle(mode="memory")

    engine = create_async_engine(
        settings.db.async_dsn,
        pool_size=settings.db.pool_size,
        pool_pre_ping=True,
        pool_recycle=3600,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    # Redis 热层（记忆 L1 / 热点缓存）；不可用时返回 None，仓储自动降级 MySQL 主存
    redis_client = build_redis_client(settings.redis)
    if redis_client is not None:
        logger.info(
            "Redis 已连接：%s:%s/%s（memory_ttl=%ss）",
            settings.redis.host, settings.redis.port, settings.redis.db, settings.redis.memory_ttl,
        )
    return DatabaseBundle(
        mode="mysql",
        engine=engine,
        redis_client=redis_client,
        session_repo=SqlSessionRepository(session_factory),
        message_repo=SqlMessageRepository(session_factory),
        document_repo=SqlDocumentRepository(session_factory),
        chunk_repo=SqlChunkRepository(session_factory),
        knowledge_base_repo=SqlKnowledgeBaseRepository(session_factory),
        metric_repo=SqlMetricRepository(session_factory),
        memory_repo=SqlMemorySummaryRepository(session_factory, redis_client, settings.redis.memory_ttl),
        slang_repo=SqlSlangRepository(session_factory),
        semantic_registry_repo=SqlSemanticRegistryRepository(session_factory),
        trace_repo=SqlTraceRepository(session_factory),
        span_repo=SqlSpanRepository(session_factory),
        few_shot_repo=SqlFewShotRepository(session_factory),
        bad_case_repo=SqlBadCaseRepository(session_factory),
        domain_memory_repo=SqlDomainMemoryRepository(session_factory),
        tenant_repo=SqlTenantRepository(session_factory),
        user_repo=SqlUserRepository(session_factory),
        role_repo=SqlRoleRepository(session_factory),
        preference_repo=SqlUserPreferenceRepository(session_factory),
        audit_log_repo=SqlAuditLogRepository(session_factory),
        error_sample_repo=SqlErrorSampleRepository(session_factory),
        conversation_state_repo=SqlConversationStateRepository(session_factory),
        plugin_store=PluginStore(session_factory),
        prompt_store=PromptVersionStore(session_factory),
        llm_config_store=LLMConfigStore(session_factory),
        metric_grant_repo=SqlMetricGrantRepository(session_factory),
        data_scope_repo=SqlDataScopeRepository(session_factory),
        dimension_policy_repo=SqlDimensionPolicyRepository(session_factory),
        semantic_published_repo=SqlSemanticPublishedRepository(session_factory),
    )
