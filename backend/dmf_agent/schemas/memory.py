"""记忆模块数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MemorySummaryData(BaseModel):
    """会话滚动摘要（每会话一条活跃记录）。"""

    session_id: str
    summary: str
    # 摘要已覆盖到的最后一条消息 ID（其后的消息不再重复进摘要）
    upto_message_id: str = ""
    updated_at: Optional[datetime] = None
    # 热层过期时间 / 最近命中时间（L1 遗忘与命中续期用；内存模式可忽略）
    expires_at: Optional[datetime] = None
    last_accessed_at: Optional[datetime] = None


class DomainMemoryData(BaseModel):
    """领域知识记忆（L3，团队 / 域级沉淀；审核制）。

    与 few_shot_examples 同源：点赞沉淀的问答对即 type='few_shot' & status='approved'，
    意图识别阶段与 few-shot 库合并做 BM25 召回 + 重排注入 prompt。
    """

    id: int = 0
    domain: str = "metric_query"
    # few_shot | rule | glossary | correction
    type: str = "few_shot"
    question: str = ""
    answer: str = ""
    intent: Optional[str] = None
    # 逗号分隔标签
    tags: str = ""
    # 频次 / 审核加权（召回重排用）
    weight: float = 1.0
    # pending | approved | rejected
    status: str = "approved"
    created_by: str = "system"
    created_at: Optional[datetime] = None


class RelevanceResult(BaseModel):
    """历史相关性判断与 query 改写结果（迁移自老系统 _check_relevance_to_history）。"""

    related: bool = Field(..., description="当前输入是否依赖历史上下文")
    rewritten_query: str = Field(..., description="改写后的独立完整问题（不相关时保持原样）")


# ---------- L2 用户偏好（PRD FR9 记忆分层 L2）----------


class UserPreferenceData(BaseModel):
    """用户偏好（L2，按用户维度长期生效，跨会话复用）。

    与 L1（会话摘要）/ L3（团队域知识）的区别：L2 是**个人**维度的稳定倾向，
    e.g. 默认时间粒度用月、习惯先看华东区、偏好表格而非长文。
    主键维度为 (user_id, key)，同一 key 只保留一条（upsert 覆盖）。

    source：
    - user     用户显式设置（管理面 / 未来"记住这条"入口），confidence 1.0
    - inferred 系统从已认可轮次自动沉淀（如点赞时记录的粒度 / 常用维度），confidence < 1
    """

    id: int = 0
    user_id: str
    tenant_id: str = ""
    # 偏好键（如 granularity / dimensions / answer_style）；DB 列名 pref_key（key 为 MySQL 保留字）
    key: str
    value: str = ""
    source: str = "user"  # user | inferred
    confidence: float = 1.0
    status: str = "active"  # active | archived
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserPreferenceView(UserPreferenceData):
    """平台视角（L2 管理面）的偏好行：附带归属用户可读名。

    仓储只存 `user_id`；路由层用用户表补 `username` / `display_name`，
    否则管理页只能看到一串 ID，无法判断这条偏好属于谁。
    """

    username: str = ""
    display_name: str = ""


class UserPreferenceUpsert(BaseModel):
    """写入 / 覆盖一条用户偏好。"""

    key: str = Field(..., min_length=1, max_length=64)
    value: str = Field("", max_length=2000)
    source: str = Field("user", pattern="^(user|inferred)$")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    # 关闭该偏好（archived）而非删除；默认 active
    status: str = Field("active", pattern="^(active|archived)$")


# ---------- L1 会话记忆（管理面列表）----------


class ManagedSummaryData(BaseModel):
    """L1 会话摘要的管理面视图：摘要 + 所属会话标题。

    表里只有 session_id（主键），管理页需要标题才能判断"这是哪个会话的记忆"，
    因此路由层用会话表补 `session_title`（一次批量查询，避免 N+1）。
    """

    session_id: str
    session_title: str = ""
    user_id: str = ""
    summary: str
    upto_message_id: str = ""
    updated_at: Optional[datetime] = None
    last_accessed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# ---------- L3 领域知识（管理面写侧）----------


class DomainMemoryUpsert(BaseModel):
    """管理员录入一条 L3 领域知识（规则 / 术语 / 纠错样本）。"""

    domain: str = Field("metric_query", max_length=32)
    type: str = Field("rule", pattern="^(few_shot|rule|glossary|correction)$")
    question: str = Field("", max_length=4000)
    answer: str = Field("", max_length=8000)
    intent: Optional[str] = Field(None, max_length=32)
    tags: str = Field("", max_length=500)
    weight: float = Field(1.0, ge=0.0, le=10.0)
    # 管理面录入默认直接通过审核（pending 用于外部渠道投稿的场景）
    status: str = Field("approved", pattern="^(pending|approved|rejected)$")


class DomainMemoryPatch(BaseModel):
    """编辑 / 审核一条 L3 领域知识（字段均可选，仅传需要改的）。

    审核即改 `status`：approved 生效（参与召回）/ rejected 驳回（不召回，保留审计）。
    """

    domain: Optional[str] = Field(None, max_length=32)
    type: Optional[str] = Field(None, pattern="^(few_shot|rule|glossary|correction)$")
    question: Optional[str] = Field(None, max_length=4000)
    answer: Optional[str] = Field(None, max_length=8000)
    intent: Optional[str] = Field(None, max_length=32)
    tags: Optional[str] = Field(None, max_length=500)
    weight: Optional[float] = Field(None, ge=0.0, le=10.0)
    status: Optional[str] = Field(None, pattern="^(pending|approved|rejected)$")
