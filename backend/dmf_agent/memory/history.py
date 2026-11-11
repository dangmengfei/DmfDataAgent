"""历史装配器：决定每轮对话实际进入 Agent 的上下文。

替代 ChatService 原来的"全量历史直塞"（token 随轮次线性膨胀），装配策略迁移自
老系统 RoundCompressor 配置语义（rounds_to_compress / rounds_to_retain /
history_relevance_check，生产配置 1/1/true）：

1. 相关性检查：当前 query 与最近 N 轮不相关 -> 截断历史，只带当前轮
   （老系统 is_related_to_history=false 时 rounds[-1:] 的语义）；
2. 滚动压缩：未压缩轮数 >= rounds_to_compress + rounds_to_retain 时，
   把超出保留窗口的轮次增量压缩进会话摘要（memory_summaries 表），
   摘要以 SystemMessage 形式注入上下文头部；
3. query 改写：相关时用改写后的独立完整问题替代原始输入（老 rewritten_query）。

摘要持久化：每次压缩即落库，进程重启不丢；压缩失败保留原轮次（不丢数据）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from dmf_agent.config.settings import Settings
from dmf_agent.db.base import MemorySummaryRepository, MessageRepository
from dmf_agent.memory.compressor import RoundCompressor, RoundData
from dmf_agent.memory.relevance import RelevanceChecker
from dmf_agent.memory.token_estimate import estimate_tokens_many
from dmf_agent.schemas.memory import MemorySummaryData
from dmf_agent.schemas.session import MessageData

logger = logging.getLogger(__name__)


@dataclass
class AssembledHistory:
    """装配结果。"""

    messages: List[BaseMessage] = field(default_factory=list)
    # 改写后的当前问题（相关且 LLM 可用时被消解为独立完整问题；否则与原输入一致）
    rewritten_query: str = ""
    related_to_history: bool = False
    summary_used: bool = False
    # 本次装配触发了滚动压缩（用于日志与 trace）
    compressed: bool = False


def pair_rounds(messages: List[MessageData]) -> List[RoundData]:
    """把有序消息列表配对为轮次（user 开轮，assistant 归属当前轮）。"""
    rounds: List[RoundData] = []
    for m in messages:
        if m.role == "user":
            rounds.append(RoundData(user_query=m.content, last_message_id=m.id))
        elif m.role == "assistant" and rounds:
            rounds[-1].assistant_answer = m.content
            rounds[-1].last_message_id = m.id
    return rounds


class HistoryAssembler:
    def __init__(
        self,
        message_repo: MessageRepository,
        summary_repo: MemorySummaryRepository,
        settings: Settings,
        get_chat_model: Callable[[], Any],
    ) -> None:
        self._message_repo = message_repo
        self._summary_repo = summary_repo
        self._settings = settings
        self._compressor = RoundCompressor(get_chat_model, settings)
        self._relevance = RelevanceChecker(get_chat_model, settings)

    async def build(
        self, session_id: str, user_message: str, callbacks: Optional[list] = None
    ) -> AssembledHistory:
        all_messages = await self._message_repo.list_by_session(session_id)

        # 记忆未启用：保持原行为（全量历史 + 原始输入）
        if not self._settings.memory.enabled:
            msgs = _to_lc_messages(all_messages)
            msgs.append(HumanMessage(content=user_message))
            return AssembledHistory(
                messages=msgs, rewritten_query=user_message, related_to_history=True
            )

        rounds = pair_rounds(all_messages)
        summary = await self._summary_repo.get(session_id)

        # 摘要覆盖之后的未压缩轮次
        uncovered = self._uncovered_rounds(rounds, summary)

        # 1. 相关性检查（无历史轮次时直接跳过）
        related = True
        rewritten = user_message
        if self._settings.memory.history_relevance_check and rounds:
            check_rounds = rounds[-self._settings.memory.relevance_check_rounds :]
            result = await self._relevance.check(check_rounds, user_message, callbacks)
            related, rewritten = result.related, result.rewritten_query
            logger.info(
                "相关性检查：related=%s rewritten=%r", related, rewritten
            )
            if not related:
                # 老系统语义：不相关 -> 截断历史（含摘要），只带当前轮
                return AssembledHistory(
                    messages=[HumanMessage(content=user_message)],
                    rewritten_query=user_message,
                    related_to_history=False,
                )

        # 2. 滚动压缩
        compress_n = self._settings.memory.rounds_to_compress
        retain_n = self._settings.memory.rounds_to_retain
        compressed = False
        if len(uncovered) >= compress_n + retain_n:
            to_compress = uncovered[:-retain_n] if retain_n else uncovered
            new_summary = await self._compressor.summarize(
                summary.summary if summary else "", to_compress, callbacks
            )
            if new_summary:
                summary = await self._summary_repo.upsert(
                    MemorySummaryData(
                        session_id=session_id,
                        summary=new_summary,
                        upto_message_id=to_compress[-1].last_message_id,
                    )
                )
                uncovered = uncovered[-retain_n:] if retain_n else []
                compressed = True
                logger.info(
                    "历史压缩完成：压缩 %s 轮，保留最近 %s 轮", len(to_compress), len(uncovered)
                )

        # 3. 组装：摘要（SystemMessage）+ 保留轮次 + 当前问题（改写后）
        msgs: List[BaseMessage] = []
        if summary and summary.summary:
            msgs.append(
                SystemMessage(content=f"此前对话摘要（供上下文参考）：\n{summary.summary}")
            )
        msgs.extend(_to_lc_messages_from_rounds(uncovered))
        msgs.append(HumanMessage(content=rewritten))

        return AssembledHistory(
            messages=msgs,
            rewritten_query=rewritten,
            related_to_history=related,
            summary_used=bool(summary and summary.summary),
            compressed=compressed,
        )

    async def _load_uncovered(self, session_id: str):
        """取该会话的全部轮次、已有摘要与其之后的未压缩轮次。"""
        all_messages = await self._message_repo.list_by_session(session_id)
        rounds = pair_rounds(all_messages)
        summary = await self._summary_repo.get(session_id)
        uncovered = self._uncovered_rounds(rounds, summary)
        return rounds, summary, uncovered

    async def context_status(self, session_id: str) -> Dict[str, Any]:
        """上下文占比状态（token 口径，供前端圆环）。

        used_tokens 为当前**实际会注入模型的历史上下文**（已有摘要 + 未压缩轮次）的
        估算 token（不含当前问句）；ratio = used_tokens / context_window_tokens（模型 max），
        clamp 到 1。轮次信息（uncovered/threshold/retain）一并返回供提示与可压缩判定。
        """
        mem = self._settings.memory
        max_tokens = self._settings.agent.context_window_tokens
        if not mem.enabled:
            return {
                "memory_enabled": False, "total_rounds": 0, "uncovered_rounds": 0,
                "retain_rounds": mem.rounds_to_retain, "threshold": 0,
                "used_tokens": 0, "max_tokens": max_tokens, "ratio": 0.0,
                "has_summary": False, "compressible": False,
            }
        rounds, summary, uncovered = await self._load_uncovered(session_id)
        used_tokens = self._context_tokens(summary, uncovered)
        ratio = min(1.0, used_tokens / max_tokens) if max_tokens > 0 else 0.0
        return {
            "memory_enabled": True,
            "total_rounds": len(rounds),
            "uncovered_rounds": len(uncovered),
            "retain_rounds": mem.rounds_to_retain,
            "threshold": mem.rounds_to_compress + mem.rounds_to_retain,
            "used_tokens": used_tokens,
            "max_tokens": max_tokens,
            "ratio": round(ratio, 4),
            "has_summary": bool(summary and summary.summary),
            # 严格多于保留窗口才有"更早轮次"可压缩
            "compressible": len(uncovered) > mem.rounds_to_retain,
        }

    @staticmethod
    def _context_tokens(
        summary: Optional[MemorySummaryData], uncovered: List[RoundData]
    ) -> int:
        """估算注入模型的历史上下文 token：摘要 + 未压缩轮次（用户问/助手答）。"""
        texts: List[str] = []
        if summary and summary.summary:
            texts.append(summary.summary)
        for r in uncovered:
            if r.user_query:
                texts.append(r.user_query)
            if r.assistant_answer:
                texts.append(r.assistant_answer)
        return estimate_tokens_many(texts)

    async def compress_now(self, session_id: str) -> Dict[str, Any]:
        """手动压缩：不待阈值，立即把更早轮次压成摘要（保留最近 retain 轮）。

        与 build() 的自动滚动压缩共用同一摘要行（按 upto_message_id 幂等）；
        LLM 不可用时 compressor 走模板摘要。轮数不足/摘要失败时 compressed=False。
        """
        mem = self._settings.memory
        if not mem.enabled:
            return {"compressed": False, "reason": "memory_disabled", **await self.context_status(session_id)}

        _rounds, summary, uncovered = await self._load_uncovered(session_id)
        retain_n = mem.rounds_to_retain
        to_compress = uncovered[:-retain_n] if retain_n else uncovered
        if not to_compress:
            return {
                "compressed": False, "reason": "nothing_to_compress",
                **await self.context_status(session_id),
            }
        new_summary = await self._compressor.summarize(
            summary.summary if summary else "", to_compress
        )
        if not new_summary:
            return {
                "compressed": False, "reason": "summarize_failed",
                **await self.context_status(session_id),
            }
        await self._summary_repo.upsert(
            MemorySummaryData(
                session_id=session_id,
                summary=new_summary,
                upto_message_id=to_compress[-1].last_message_id,
            )
        )
        logger.info("手动上下文压缩：session=%s 压缩 %s 轮，保留最近 %s 轮",
                    session_id, len(to_compress), retain_n)
        return {
            "compressed": True, "rounds_compressed": len(to_compress),
            **await self.context_status(session_id),
        }

    @staticmethod
    def _uncovered_rounds(
        rounds: List[RoundData], summary: Optional[MemorySummaryData]
    ) -> List[RoundData]:
        if summary is None or not summary.upto_message_id:
            return list(rounds)
        for idx, r in enumerate(rounds):
            if r.last_message_id == summary.upto_message_id:
                return rounds[idx + 1 :]
        return list(rounds)

def _to_lc_messages(messages: List[MessageData]) -> List[BaseMessage]:
    msgs: List[BaseMessage] = []
    for m in messages:
        if m.role == "user":
            msgs.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            msgs.append(AIMessage(content=m.content))
    return msgs


def _to_lc_messages_from_rounds(rounds: List[RoundData]) -> List[BaseMessage]:
    msgs: List[BaseMessage] = []
    for r in rounds:
        msgs.append(HumanMessage(content=r.user_query))
        if r.assistant_answer:
            msgs.append(AIMessage(content=r.assistant_answer))
    return msgs
