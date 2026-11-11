"""SQLAlchemy ORM 模型（MySQL）。

对应老系统 MySQL 中的 aigc_* 系列表；v2 重新建模，
会话/消息迁移自 metadata/sa_meta_convert.py 的读写逻辑。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    func,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # 所属租户（多租户隔离：会话列表按 tenant_id 过滤，跨租户不可见）
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # 过程追踪：意图/要素/DSL 等，对应老系统 sa_message.round_information
    node_trace: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class KnowledgeDocumentModel(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="text")
    source_uri: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|indexed|failed
    chunk_count: Mapped[int] = mapped_column(default=0)
    knowledge_base_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )  # 归属知识库（None 表示未归类/全局）
    # 来源分级（FR7 知识库分级）：authoritative 权威源 | sediment 沉淀源 | user 用户上传
    # 索引用于「按分级过滤/统计」；检索时按分级加权（见 rag/retriever.py）。
    authority_level: Mapped[str] = mapped_column(
        String(16), default="user", index=True
    )
    # 所属租户（多租户隔离：未归类文档同样带租户，避免"孤儿文档"跨租户可见）
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 文档归属用户：个人/私有知识库下的文档 owner_id = 上传者；企业/公开库或系统权威源 = ""。
    # 检索时与 tenant_id 配合做**个人/企业隔离**——同租户内只有 owner 能看到自己的私有文档，
    # 防止 A 的个人私有库被 B 检索到（见 rag/retriever.py）。
    owner_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBaseModel(Base):
    """知识库（个人 / 企业）。文档通过 knowledge_base_id 归属到某个知识库。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # personal: 个人知识库（owner_id 归属某用户）；enterprise: 企业共享知识库
    type: Mapped[str] = mapped_column(String(16), default="personal", index=True)
    owner_id: Mapped[str] = mapped_column(String(64), default="")  # 个人知识库归属用户；企业可空
    # 所属租户（多租户隔离：知识库列表按 tenant_id 过滤，跨租户不可见）
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # private: 仅 owner 可见；public: 全员可见（企业知识库通常 public）
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeChunkModel(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(default=0)
    content: Mapped[str] = mapped_column(Text)
    vector_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ConversationStateModel(Base):
    """结构化对话状态（P0-6 多轮对话状态管理）。

    与 `ConversationStateData` 对应：每轮成功查询后由 `ConversationStateService`
    落库，捕获已解析实体（指标 / 维度 / 过滤 / 时间窗）供下一轮确定性指代消解。
    """

    __tablename__ = "conversation_states"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 最近一轮意图
    intent: Mapped[str] = mapped_column(String(32), default="")
    # 已解析实体（JSON 列，MySQL 仅 TEXT/JSON 不能 server_default，用 Python 侧默认）
    active_metrics: Mapped[list] = mapped_column(JSON, default=list)
    active_dimensions: Mapped[list] = mapped_column(JSON, default=list)
    active_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    time_range: Mapped[dict] = mapped_column(JSON, default=dict)
    # 主题摘要
    topic: Mapped[str] = mapped_column(String(255), default="")
    # 最近一轮原始 / 改写问句
    last_query: Mapped[str] = mapped_column(Text, default="")
    last_query_rewritten: Mapped[str] = mapped_column(Text, default="")
    # 挂起澄清
    pending_clarification: Mapped[dict] = mapped_column(JSON, default=dict)
    # 累计成功轮次
    turn_count: Mapped[int] = mapped_column(default=0)
    # 反问澄清累计次数（FR4 上限控制）：澄清序列内每追问一次 +1，成功解决后清零
    clarification_count: Mapped[int] = mapped_column(default=0)
    # 状态机：active | awaiting_clarification | idle
    status: Mapped[str] = mapped_column(String(32), default="active")
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MemorySummaryModel(Base):
    """会话滚动摘要（对话记忆压缩，迁移自老系统 RoundCompressor 的 previous_summary）。

    L1 会话记忆：MySQL 主存 + Redis 热层（TTL）。expires_at / last_accessed_at
    用于遗忘（超时会话级过期）与命中续期；Redis 不可用时仅依赖 MySQL 主存。
    """

    __tablename__ = "memory_summaries"

    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text)
    # 摘要已覆盖到的最后一条消息 ID
    upto_message_id: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # 热层过期时间（会话级 TTL；NULL 表示依赖 MySQL 主存不过期）
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 最近命中时间（遗忘：长时间未访问则降为候选 / 超时清理）
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MetricDefinitionModel(Base):
    """指标元数据（迁移自老系统 metadata/hm_meta 指标定义与配置）。"""

    __tablename__ = "metric_definitions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(64), default="", index=True)
    dimensions: Mapped[List[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SlangEntryModel(Base):
    """黑话词典条目（三级：行业级 / 公司级 / 用户级）。

    统一承载 PRD §5.3.1 的「黑话字典结构」：term -> standard，按
    entry_type（metric/dimension/dim_value/term/stop）区分语义，
    按 rewrite_mode（expand/replace/stop）区分改写行为。

    旧表 `cant_entries`（老 aigc_cant_name）已由迁移 0010/0011 迁入本表，
    并于 0011 删除；本表是其统一升级版（多了 scope / 指标黑话 /
    术语 / 停用词 / 优先级 / 状态 / 版本）。
    """

    __tablename__ = "slang_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # industry（行业级，tenant/user 为空）/ tenant（公司级）/ user（用户级）
    scope: Mapped[str] = mapped_column(String(16), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 匹配源：黑话 / 口语 / 缩写
    term: Mapped[str] = mapped_column(String(128), index=True)
    # 标准名（指标名 / 维度名 / 术语；stop 类型为空）
    standard: Mapped[str] = mapped_column(String(255), default="")
    # metric | dimension | dim_value | term | stop
    entry_type: Mapped[str] = mapped_column(String(16), default="term", index=True)
    # entry_type=dim_value 时逗号分隔的维值
    dim_value: Mapped[str] = mapped_column(String(1024), default="")
    # expand | replace | stop
    rewrite_mode: Mapped[str] = mapped_column(String(16), default="expand")
    # 同层冲突时的优先级（数值越大越优先）
    priority: Mapped[int] = mapped_column(default=100)
    # builtin | manual | imported
    source: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    # draft | published | disabled（仅 published 参与线上解析）
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    version: Mapped[int] = mapped_column(default=1)
    confidence: Mapped[float] = mapped_column(default=1.0)
    hit_count: Mapped[int] = mapped_column(default=0)
    remark: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # 同层同类型同词唯一（避免同一层级堆叠重复词条）
        UniqueConstraint(
            "scope", "tenant_id", "user_id", "entry_type", "term", name="uk_slang"
        ),
    )


class SlangEntryVersionModel(Base):
    """黑话词条版本审计（每次变更留一份前后快照，支持版本对比与回滚）。"""

    __tablename__ = "slang_entry_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    # create | update | publish | disable | delete | rollback
    change_type: Mapped[str] = mapped_column(String(16), default="update")
    before_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SlangRewriteEventModel(Base):
    """查询改写行为埋点（命中率 / 误改写率看板数据源）。

    每次改写节点执行后 best-effort 落一条：
    - `hit_count` / `is_rewritten` 用于算**命中率**；
    - `feedback`（1=正确 / -1=误改写 / NULL=未评价）由前端改写徽标 👍/👎 回写，
      用于算**误改写率**。
    `query_hash` = 归一化后问句的哈希，作为"同一问句"的反馈去重键。
    """

    __tablename__ = "slang_rewrite_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(64), default="")
    # 归一化问句哈希（feedback 去重键；不含 PII，仅用于聚合）
    query_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 截断后的原始问句（便于排查，非完整日志）
    query_text: Mapped[str] = mapped_column(String(512), default="")
    hit_count: Mapped[int] = mapped_column(default=0)
    is_rewritten: Mapped[int] = mapped_column(default=0)  # 0/1 布尔
    # 1=改写正确 / -1=误改写 / NULL=未评价
    feedback: Mapped[Optional[int]] = mapped_column(default=None, nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(255), default="")


# ============================================================================
# 语义注册中心（P1 骨架，见 doc/语义层注册中心与指标权限治理方案.md）
# 真源切换（DB 优先 + 热更新）在 P3；当前运行时仍读 manifest.yml，
# 注册中心先作为"有稳定身份（*_key）、可校验、可治理"的只读镜像 + 导入种子。
# ============================================================================


class SemanticModelModel(Base):
    """语义模型注册（对应 manifest.yml 的 semantic_models，物理表 + 维度 + 度量容器）。"""

    __tablename__ = "semantic_models"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # industry（一期唯一；预留 tenant）
    scope: Mapped[str] = mapped_column(String(16), default="industry", index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 不可变稳定键（= manifest 模型名，英文 snake_case）
    model_key: Mapped[str] = mapped_column(String(128), index=True)
    # 展示名（可改；一期与 key 相同）
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    table: Mapped[str] = mapped_column(String(255))
    agg_time_dimension: Mapped[str] = mapped_column(String(128), default="")
    entities: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    # draft | review | published | deprecated
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    version: Mapped[int] = mapped_column(default=1)
    # builtin | imported | manual
    source: Mapped[str] = mapped_column(String(32), default="builtin", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("scope", "tenant_id", "model_key", name="uk_sem_model"),
    )


class SemanticDimensionModel(Base):
    """维度注册（挂在语义模型下；dimension_key 取物理 expr，稳定且英文）。"""

    __tablename__ = "semantic_dimensions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(index=True)
    dimension_key: Mapped[str] = mapped_column(String(128))
    # 业务名（中文，可改；运行时按 name 解析）
    name: Mapped[str] = mapped_column(String(255), index=True)
    # time | categorical
    dim_type: Mapped[str] = mapped_column(String(16), default="categorical")
    expr: Mapped[str] = mapped_column(String(255))
    time_granularity: Mapped[str] = mapped_column(String(16), default="day")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    version: Mapped[int] = mapped_column(default=1)
    # 审批人留痕（发布 / 回滚时回填），便于审计「谁审批了哪个版本」
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("model_id", "dimension_key", name="uk_sem_dimension"),
    )


class SemanticMeasureModel(Base):
    """度量注册（指标的物理载体；measure_key = manifest 度量名，英文不可变）。"""

    __tablename__ = "semantic_measures"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(index=True)
    measure_key: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    # sum | avg | max | min | count | count_distinct | median
    agg: Mapped[str] = mapped_column(String(32), default="sum")
    expr: Mapped[str] = mapped_column(String(255))
    agg_time_dimension: Mapped[str] = mapped_column(String(128), default="")
    unit: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("model_id", "measure_key", name="uk_sem_measure"),
    )


class SemanticMetricModel(Base):
    """指标注册（对应 manifest.yml 的 metrics；权限/血缘/审计的锚点都是 metric_key）。"""

    __tablename__ = "semantic_metrics"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), default="industry", index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 不可变稳定键：simple=度量名 / ratio={分子key}__{分母key}_ratio / derived=derived_{hash8}
    metric_key: Mapped[str] = mapped_column(String(255), index=True)
    # 业务名（中文，可改；运行时按 name 解析，改名不断引用）
    name: Mapped[str] = mapped_column(String(255), index=True)
    # simple | ratio | derived
    metric_type: Mapped[str] = mapped_column(String(16), default="simple", index=True)
    # {measure} / {numerator, denominator} / {expr, metrics}
    type_params: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    aliases: Mapped[List[str]] = mapped_column(JSON, default=list)
    unit: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    scale: Mapped[float] = mapped_column(default=1.0)
    round: Mapped[int] = mapped_column(default=4)
    granularities: Mapped[List[str]] = mapped_column(JSON, default=list)
    pop_types: Mapped[List[str]] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(64), default="")
    # draft | review | published | deprecated
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    version: Mapped[int] = mapped_column(default=1)
    source: Mapped[str] = mapped_column(String(32), default="builtin", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    # 审批人留痕（发布 / 回滚时回填），便于审计「谁审批了哪个版本」
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    # 发布口径的语义模型 manifest 兼容 dict（PRD 5.9 闭环）：发布指标生成并落库 yaml_defn，
    # 供知识库口径字典增量重建与可观测性。空 dict 表示尚未发布或草稿期未生成。
    # 允许 NULL（历史行迁移后为 NULL），读取侧一律 `or {}` 兜底，避免 MySQL 对 JSON 列禁设 DB 默认值的限制。
    yaml_defn: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("scope", "tenant_id", "metric_key", name="uk_sem_metric"),
    )


class SemanticMetricVersionModel(Base):
    """指标版本审计（P2 的状态机/回滚使用；P1 建表占位，随导入写入首版快照）。"""

    __tablename__ = "semantic_metric_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    # create | update | submit | publish | deprecate | rollback | import
    change_type: Mapped[str] = mapped_column(String(16), default="import")
    before_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    operator: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SemanticDimensionVersionModel(Base):
    """维度版本审计（P2：与指标对称支持状态机/回滚）。"""

    __tablename__ = "semantic_dimension_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dimension_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    # create | update | submit | publish | deprecate | rollback
    change_type: Mapped[str] = mapped_column(String(16), default="create")
    before_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    operator: Mapped[str] = mapped_column(String(64), default="")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TraceRecordModel(Base):
    """对话观测 trace（每轮一条，前端"运行观测"面板数据源）。"""

    __tablename__ = "trace_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    round_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    mock_mode: Mapped[bool] = mapped_column(default=False)
    skills: Mapped[List[Any]] = mapped_column(JSON, default=list)
    elapsed_ms: Mapped[Optional[float]] = mapped_column(nullable=True)
    has_error: Mapped[bool] = mapped_column(default=False, index=True)
    # token 消耗（从 LLM 响应 usage 中累计；mock 模式为 None）
    prompt_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(nullable=True, index=True)
    # ---- FR11 成本与归属（迁移 0021；历史行回填空串 / 0，无破坏性） ----
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 本轮使用的 LLM 模型名（成本按模型归集；空 = 未记录）
    model: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 本轮估算成本（币种见 DMF_COST-CURRENCY；未启用计价为 0）
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    trace: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class TraceSpanModel(Base):
    """OpenTelemetry span（每轮对话有多条：root `chat_round` + 各节点 `node.<name>`）。

    字段对齐 OTel Span 规范；trace_id / span_id / parent_span_id 由 OTel SDK 生成。
    轮级汇总仍存 trace_records（该表只做增量新增，不改动既有表结构）。
    """

    __tablename__ = "trace_spans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(16), index=True)
    parent_span_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    round_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="INTERNAL")
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[float]] = mapped_column(nullable=True)
    status_code: Mapped[str] = mapped_column(String(16), default="UNSET", index=True)
    status_message: Mapped[str] = mapped_column(String(512), default="")
    # 列名 attributes；Python 属性避开 SQLAlchemy 声明式保留名风险
    span_attributes: Mapped[Dict[str, Any]] = mapped_column(
        "attributes", JSON, default=dict
    )
    events: Mapped[List[Any]] = mapped_column(JSON, default=list)
    service_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class PluginModel(Base):
    """插件注册表（Skill / MCP Server）。

    内置 Skill 由代码内置（只读展示，不落库）；用户自定义的 Skill / MCP 落此表。
    """

    __tablename__ = "plugins"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plugin_type: Mapped[str] = mapped_column(String(16), index=True)  # skill | mcp
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    endpoint: Mapped[str] = mapped_column(String(512), default="")
    config: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="enabled")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class LLMConfigModel(Base):
    """对话 LLM 模型配置（模型管理页）。

    可维护多条 OpenAI 兼容的对话模型（qwen / zhipu / deepseek / vllm / ollama）。
    ``is_default`` 且 ``status=enabled`` 的条目作为**运行时生效模型**，覆盖环境变量
    ``DMF_LLM-*``（未配置默认模型时回落环境变量，见 ``services/models.py::ModelRegistry``）。
    ``api_key`` 明文存储（连接测试需真实 key），列表 / 详情接口返回时脱敏。
    ``last_test`` 存连接测试结果 JSON（ok / elapsed_ms / at / error）。
    """

    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 模型标识（唯一）
    model: Mapped[str] = mapped_column(String(128), default="")  # 模型名，如 qwen-max
    base_url: Mapped[str] = mapped_column(String(512), default="")  # OpenAI 兼容端点
    api_key: Mapped[str] = mapped_column(String(512), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    timeout: Mapped[int] = mapped_column(Integer, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # 运行时生效标记
    status: Mapped[str] = mapped_column(String(16), default="enabled")  # enabled | disabled
    last_test: Mapped[str] = mapped_column(Text, default="{}")  # 连接测试结果 JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class PromptVersionModel(Base):
    """系统提示词版本（历史不可变；status=published 为当前生效版本）。

    回滚采用"复制历史内容生成新版本"，保留完整审计链（source_version 记录来源版本）。
    """

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16))
    system: Mapped[str] = mapped_column(Text)
    human: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    source_version: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # ---- FR12：5 层标注 + 灰度 + 评测门禁 ----
    layer: Mapped[str] = mapped_column(String(16), default="", index=True)
    rollout_status: Mapped[str] = mapped_column(String(16), default="full", index=True)
    gray_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    eval_passed: Mapped[bool] = mapped_column(Boolean, default=False)


class FewShotExampleModel(Base):
    """few-shot 样本库（意图识别少样本示例的数据源；管理面「Few-Shot 管理」页）。

    两种来源共用本表，由 `source` 区分：
    - like   用户点赞助手回答时自动沉淀：question（用户原始问题）/ answer（助手最终回答）
             / intent（本轮识别意图）/ session_id + message_id（溯源，重复点赞累加 score）；
    - manual 管理面手工录入 / 批量导入（session_id / message_id 为空）。

    意图识别阶段按 BM25 相似度召回同类问题，把命中样本的意图期望输出注入 prompt
    （category 对齐 experience.store.ExampleStore 的 "intention"）。
    `status=disabled` 的样本不参与召回（管理面停用而非删除，保留审计）。
    """

    __tablename__ = "few_shot_examples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 被点赞的助手消息 id（即 trace_records.round_id），便于溯源与去重；手工样本为空
    message_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # few-shot 类别（意图识别用 "intention"；其他技能/要素可扩展）
    category: Mapped[str] = mapped_column(String(32), default="intention", index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    # 点赞权重（同一消息被多人/多次点赞时累加；手工样本恒为 1）
    score: Mapped[int] = mapped_column(default=1)
    # like | manual（点赞沉淀 / 手工录入，见类注释）
    source: Mapped[str] = mapped_column(String(16), default="like", index=True)
    # active | disabled（disabled 不参与召回）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 召回重排权重（与 domain_memories.weight 同义；点赞样本默认 1.0）
    weight: Mapped[float] = mapped_column(default=1.0)
    # 逗号分隔标签（管理面筛选 / 备注）
    tags: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class BadCaseModel(Base):
    """点踩样本库（bad case）。

    用户点踩助手回答时写入一条：question / answer / intent / 点踩原因。
    当前仅落库用于后续分析与回流（无消费方），结构保留以便后期接入训练/评测。
    """

    __tablename__ = "bad_cases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    # 点踩原因（前端可选填；未填为 None）
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DomainMemoryModel(Base):
    """领域知识记忆（L3，团队 / 域级，长期沉淀，审核制）。

    few_shot_examples（点赞样本库）视为本表的「已验证样本」视图（type='few_shot' &
    status='approved'）；点赞时并行写入本表（type=few_shot）作为 L3 沉淀，二者在意图
    识别阶段合并做 BM25 召回（`memory/retriever.py` 按（问题, 意图）去重，避免同一
    问答对被召回两次）。本表还承载更泛化的规则 / 术语 / 纠错样本
    （type=rule|glossary|correction），由管理员在「记忆管理」页审核后生效。
    """

    __tablename__ = "domain_memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(32), default="metric_query", index=True)
    # few_shot | rule | glossary | correction
    type: Mapped[str] = mapped_column(String(32), default="few_shot", index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 逗号分隔标签
    tags: Mapped[str] = mapped_column(Text, default="")
    # 频次 / 审核加权（召回重排用）
    weight: Mapped[float] = mapped_column(default=1.0)
    # pending | approved | rejected（管理员审核；点赞沉淀默认 approved）
    status: Mapped[str] = mapped_column(String(16), default="approved", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class UserPreferenceModel(Base):
    """用户偏好（L2，个人维度长期记忆，跨会话生效）。

    主键维度 (user_id, pref_key)：同一用户同一偏好只保留一条（upsert 覆盖）。
    DB 列名用 pref_key 而非 key —— `key` 是 MySQL 保留字，避免各层反复加引号。
    source: user（用户显式设置）| inferred（系统从已认可轮次沉淀）。
    """

    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "pref_key", name="uq_user_preferences_user_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    pref_key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="user", index=True)
    confidence: Mapped[float] = mapped_column(default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TenantModel(Base):
    """租户（多租户隔离的最上层）：用户归属于租户，会话 / 知识库按租户隔离。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserModel(Base):
    """用户：登录主体，归属某租户并持有角色（admin/analyst/viewer）。

    口令以 PBKDF2-HMAC-SHA256 哈希存储（`pbkdf2_sha256$iter$salt$hash`），不存明文。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer", index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class RoleModel(Base):
    """角色（RBAC）：name 为标识（admin/analyst/viewer 内置 + 自定义）。

    permissions 存能力标签数组（admin 为 ["*"]；自定义角色只能勾选 ALL_PERMISSIONS 子集）。
    is_builtin=True 的角色不可删、权限不可改（防锁死/意外提权）。
    """

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|disabled
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ============================================================================
# 合规审计（PRD FR13）：全链路审计日志 + 内容安全/权限/脱敏事件
# ============================================================================


class AuditLogModel(Base):
    """审计日志：谁在何时查了什么指标、返回什么、token 消耗；记忆与兜底创建留痕。

    - event_type：security/input_guard | security/output_guard | harness/tool_call |
                   permission/check | data_access | memory/write | memory/read |
                   metric/create | dimension/create | model/create
    - level：info | warning | high（越权/注入/拦截）
    - detail_json：结构化详情（指标列表、过滤条件、命中规则、脱敏/越权明细等）
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)  # info|warning|high
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    round_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 可读摘要（管理面列表展示）
    summary: Mapped[str] = mapped_column(String(512), default="")
    # 结构化详情
    detail_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # 客户端 IP / 请求 UA 等（可选）
    client_info: Mapped[str] = mapped_column(String(255), default="")


# ============================================================================
# 错误样本池（PRD FR6）：报错分类 + 限次自愈的样本落库（观测/分析自愈成功率）
# ============================================================================


class ErrorSampleModel(Base):
    """错误样本：每次（节点级或服务级）被分类的 AgentError 落一条。

    - error_code：E1xxx–E5xxx（解析/编译/权限/数据源/执行）
    - category：1–5（与错误码首位一致）
    - recovered：是否经限次自愈恢复（True=自愈成功，False=最终失败）
    - retryable / fix_hint：是否可重试与修复策略
    """

    __tablename__ = "error_samples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    round_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    error_code: Mapped[str] = mapped_column(String(16), default="", index=True)
    category: Mapped[int] = mapped_column(Integer, default=0, index=True)
    category_label: Mapped[str] = mapped_column(String(16), default="")
    node: Mapped[str] = mapped_column(String(48), default="", index=True)
    message: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    fix_hint: Mapped[str] = mapped_column(String(32), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    trace_snippet: Mapped[str] = mapped_column(Text, default="")


class MetricGrantModel(Base):
    """指标授权（PRD FR13 P5/P6 enforce）。

    语义：空表 = 不限制（所有用户可见全部指标）；一旦某 user_id / role 有授权记录，
    则该主体**只可见被授予的指标**（白名单语义）。user_id 级优先于 role 级合并。
    """

    __tablename__ = "metric_grants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 授权主体：user_id 与 role 二选一（都留空表示全租户角色级）
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="", index=True)
    metric_key: Mapped[str] = mapped_column(String(255), index=True)  # 稳定键（与 manifest 一致）
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_metric_grant_lookup", "tenant_id", "user_id", "role"),
    )


class DataScopeModel(Base):
    """行权限（PRD FR13 P6 enforce）：用户/角色只能查看某维度的某些值。

    下推到 SQL 最内层 WHERE `t.<维度列> IN (...)`；空表 = 无行限制。
    """

    __tablename__ = "data_scopes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="", index=True)
    dimension: Mapped[str] = mapped_column(String(128), index=True)  # 维度别名（如 branch_name）
    allowed_values: Mapped[List[str]] = mapped_column(JSON, default=list)  # 允许值集合
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_data_scope_lookup", "tenant_id", "user_id", "role"),
    )


class DimensionPolicyModel(Base):
    """列/维度 deny 策略（PRD FR13 P6 enforce）：用户/角色被禁止查看某些维度列。

    结果集中这些列被**硬剔除**（区别于脱敏掩码：掩码仍可见脱敏值，deny 是直接不出列）。
    空表 = 无列限制。denied=True 表示禁止；denied=False 可用于显式放行（覆盖默认）。
    """

    __tablename__ = "dimension_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="", index=True)
    dimension: Mapped[str] = mapped_column(String(128), index=True)
    denied: Mapped[bool] = mapped_column(Boolean, default=True)
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_dim_policy_lookup", "tenant_id", "user_id", "role"),
    )


class SemanticPublishedModel(Base):
    """语义层发布快照（混合方案：YAML 基座 → DB 发布 → 运行时装配）。

    一条记录 = 某租户某版本的「已合并 manifest 原始字典」快照（payload）。
    - tenant_id=""：全局默认快照（所有租户共享）；
    - state 复用语义注册中心状态机 draft→review→published→deprecated；
    - mode 区分 full（整份快照）/ overlay（只含差异条目，用于局部租户覆盖）；
    - 运行时取数只消费 state=published 的最新版本，无该租户快照时回落全局。
    """

    __tablename__ = "semantic_published"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # full = 整份已合并 manifest；overlay = 只含被改动的 model/metric 条目（局部租户覆盖）
    mode: Mapped[str] = mapped_column(String(16), default="full", server_default="full")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 审批审计：approved_by 在 review→published 时记录；rejected_by/reject_reason 在驳回时记录
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    rejected_by: Mapped[str] = mapped_column(String(64), default="")
    reject_reason: Mapped[str] = mapped_column(String(512), default="")

    __table_args__ = (
        Index("ix_semantic_pub_lookup", "tenant_id", "state", "version"),
    )
