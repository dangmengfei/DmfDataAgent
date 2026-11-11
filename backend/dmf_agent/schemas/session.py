"""会话 / 消息模型（API DTO 与仓储层共用）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SessionData(BaseModel):
    id: str
    user_id: str
    tenant_id: str = ""
    title: str = "新会话"
    status: str = "active"
    created_at: datetime
    updated_at: Optional[datetime] = None


class SessionCreate(BaseModel):
    user_id: str = "anonymous"
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str = Field(..., min_length=1)


class MessageData(BaseModel):
    id: str
    session_id: str
    role: str  # user | assistant
    content: str
    # 过程追踪数据（意图 / 要素 / DSL 等），对应老系统 sa_message.round_information
    node_trace: dict = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class ConversationStateData(BaseModel):
    """结构化对话状态（P0-6 多轮对话状态管理）。

    每轮成功查询后由 `ConversationStateService.snapshot_from` 落库，捕获本轮
    **已解析实体**（指标 / 维度 / 过滤 / 时间窗 / 主题 / 原始与改写问句 / 挂起澄清），
    下一轮由 `ChatService` 注入 `context_entities` 供要素抽取与改写节点做**确定性指代消解**
    （"它 / 这个指标 / 按地区拆分 / 和上个月比" 不再只依赖模糊的 LLM 改写）。
    """

    session_id: str
    tenant_id: str = ""
    user_id: str = ""
    # 最近一轮意图（metric_query / dimension_query / diagnosis / knowledge_qa / chitchat）
    intent: str = ""
    # 已解析实体（来自最后一轮成功的 query_elements）
    active_metrics: list = Field(default_factory=list)
    active_dimensions: list = Field(default_factory=list)
    active_filters: dict = Field(default_factory=dict)
    # 时间窗（granularity / start / end / period_comparison 等）
    time_range: dict = Field(default_factory=dict)
    # 主题摘要（如"存款余额查询"），用于无关历史截断时的兜底提示
    topic: str = ""
    # 最近一轮原始问句与改写后问句（审计 + 指代消解输入）
    last_query: str = ""
    last_query_rewritten: str = ""
    # 挂起澄清（ask_clarification 产出：{problems, original_query}）
    pending_clarification: dict = Field(default_factory=dict)
    # 累计轮次（成功查询计数，不含纯澄清应答）
    turn_count: int = 0
    # 反问澄清累计次数（FR4 上限控制）：同一澄清序列内每追问一次 +1，成功解决后清零
    clarification_count: int = 0
    # 状态机：active（常规）| awaiting_clarification（挂起确认）| idle（已重置）
    status: str = "active"
    updated_at: Optional[datetime] = None

