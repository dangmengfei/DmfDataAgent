"""结构化对话状态服务（P0-6 多轮对话状态管理）。

职责：
1. **快照落库**（`snapshot_from`）：每轮成功查询后，从图最终状态抽取已解析实体
   （intent / metrics / dimensions / filters / time_range / last_query / 挂起澄清），
   与上一轮状态合并后写回 `conversation_states`，并维护状态机
   （active ↔ awaiting_clarification）；
2. **上下文读取**（`load_context`）：下一轮开始前读取上一轮状态，供 ChatService
   注入 `context_entities`，使要素抽取 / 改写节点能对"它 / 这个指标 / 按地区拆分 /
   和上个月比"做**确定性指代消解**，而不只依赖 LLM 模糊改写；
3. **重置**（`reset`）：清空会话状态（用户主动重置上下文）。

与 `HistoryAssembler` 的「相关性检查 + 滚动压缩 + LLM 改写」互补：
- HistoryAssembler 解决"带哪些历史进上下文"（横向、跨轮）；
- 本服务解决"上一轮解析出的实体是什么"（纵向、结构化），是确定性的，
  不引入额外 LLM 调用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from dmf_agent.db.base import ConversationStateRepository
from dmf_agent.schemas.session import ConversationStateData

logger = logging.getLogger(__name__)

# 状态机：active（常规）| awaiting_clarification（挂起确认）| idle（已重置）
STATUS_ACTIVE = "active"
STATUS_AWAITING = "awaiting_clarification"
STATUS_IDLE = "idle"

# 仅当本轮解析出这些字段非空时，才覆盖上一轮的对应实体（空缺不回退）
_ENTITY_FIELDS = ("active_metrics", "active_dimensions", "active_filters", "time_range")


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return bool(value)


class ConversationStateService:
    def __init__(self, repo: ConversationStateRepository) -> None:
        self._repo = repo

    async def get(self, session_id: str) -> Optional[ConversationStateData]:
        return await self._repo.get(session_id)

    async def load_context(
        self, session_id: str
    ) -> Optional[Dict[str, Any]]:
        """读取上一轮上下文，返回注入要素抽取 / 改写节点的 `context_entities` 提示。

        仅当状态为 active（非挂起确认、非空闲）且携带实体时返回，避免把过期的
        挂起澄清或重置后的空状态当作有效上下文。
        """
        state = await self._repo.get(session_id)
        if state is None or state.status != STATUS_ACTIVE:
            return None
        if not (state.active_metrics or state.active_dimensions or state.topic):
            return None
        return {
            "intent": state.intent,
            "active_metrics": list(state.active_metrics or []),
            "active_dimensions": list(state.active_dimensions or []),
            "active_filters": dict(state.active_filters or {}),
            "time_range": dict(state.time_range or {}),
            "topic": state.topic or "",
            "turn_count": state.turn_count or 0,
        }

    async def snapshot_from(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        intent: str = "",
        query_elements: Optional[Dict[str, Any]] = None,
        query_original: str = "",
        query_rewritten: str = "",
        confirmation: Optional[Dict[str, Any]] = None,
    ) -> ConversationStateData:
        """从本轮图状态抽取实体并合并落库。

        `query_elements` 取最后一轮 `extract_elements` 产出；`confirmation` 取
        `ask_clarification` 产出（含 original_query 表示挂起确认）。
        """
        query_elements = query_elements or {}
        prior = await self._repo.get(session_id)

        # 实体：本轮解析非空才覆盖；空缺沿用上一轮（追问常只补维度/过滤）
        merged: Dict[str, Any] = {}
        for field in _ENTITY_FIELDS:
            cur = query_elements.get(field)
            if _non_empty(cur):
                merged[field] = cur
            elif prior is not None:
                merged[field] = getattr(prior, field)
            else:
                merged[field] = [] if field in ("active_metrics", "active_dimensions") else {}

        topic = query_elements.get("topic") or (prior.topic if prior else "") or (
            query_original[:40] if query_original else ""
        )
        last_query = query_original or (prior.last_query if prior else "")
        last_rewritten = query_rewritten or (prior.last_query_rewritten if prior else "")

        # 挂起确认 -> awaiting_clarification；否则 active
        if confirmation and confirmation.get("original_query"):
            status = STATUS_AWAITING
            pending = confirmation
            turn_count = prior.turn_count if prior else 0
            # 反问澄清累计 +1（FR4 上限由 ask_clarification 节点读取判定）
            clarification_count = (prior.clarification_count if prior else 0) + 1
        else:
            status = STATUS_ACTIVE
            pending = {}
            # 成功查询轮次 +1（澄清应答不计数）
            turn_count = (prior.turn_count if prior else 0) + 1
            # 澄清序列已解决（成功查询 / 达上限优雅终止），清零计数
            clarification_count = 0

        data = ConversationStateData(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            intent=intent or (prior.intent if prior else ""),
            active_metrics=list(merged.get("active_metrics") or []),
            active_dimensions=list(merged.get("active_dimensions") or []),
            active_filters=dict(merged.get("active_filters") or {}),
            time_range=dict(merged.get("time_range") or {}),
            topic=topic,
            last_query=last_query,
            last_query_rewritten=last_rewritten,
            pending_clarification=pending,
            turn_count=turn_count,
            clarification_count=clarification_count,
            status=status,
        )
        return await self._repo.upsert(data)

    async def reset(self, session_id: str) -> bool:
        """清空会话状态（用户主动重置上下文）。返回是否原本存在。"""
        existed = await self._repo.get(session_id) is not None
        if existed:
            await self._repo.delete(session_id)
        return existed
