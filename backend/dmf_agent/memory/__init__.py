"""对话记忆模块（2026-08-29 迁移自老系统 memory/）。

- compressor.py : 滚动轮次压缩（老系统 RoundCompressor 的 previous_summary 增量摘要）
- relevance.py  : 历史相关性检查 + query 改写（老系统 _check_relevance_to_history，
                  指代消解为独立完整问题）
- history.py    : 历史装配器（替代 ChatService 原来的全量历史直塞）

老系统经验库（memory/experience.py + sa_utils/experience_mgr.py）
属 few-shot 召回，由 experience/ 模块（ExampleStore）承接。
"""

from dmf_agent.memory.compressor import RoundCompressor
from dmf_agent.memory.history import AssembledHistory, HistoryAssembler
from dmf_agent.memory.relevance import RelevanceChecker
from dmf_agent.memory.retriever import (
    INJECT_HEADER,
    MemoryRetriever,
    RecallOutcome,
    RecalledSample,
)

__all__ = [
    "AssembledHistory",
    "HistoryAssembler",
    "INJECT_HEADER",
    "RelevanceChecker",
    "RoundCompressor",
    "MemoryRetriever",
    "RecallOutcome",
    "RecalledSample",
]
