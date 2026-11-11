"""历史相关性检查 + query 改写。

迁移自老系统 memory/compression.py::_check_relevance_to_history：
- 判断当前输入是否依赖历史上下文（指代"它/上述/继续"、省略主语、追加条件等）；
- 相关时把当前输入改写为不依赖上下文的独立完整问题（老系统 rewritten_query，
  供要素抽取等后续节点使用，替代直接携带完整历史）；
- 老系统的熵校验兜底（改写后信息熵增大则视为相关）简化为：改写后比原文更长即视为相关；
- LLM 不可用时降级为规则判断（指代词 + 字符 bigram 重叠率），不做改写。

老系统定制（数据解读/可视化轮次剔除、post_message 数据比对）暂不迁移，
待 Phase 3 取数链路迁移后按需补充。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.config.settings import Settings
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.memory.compressor import RoundData
from dmf_agent.schemas.memory import RelevanceResult

logger = logging.getLogger(__name__)

# 指代/追加词：命中即认为依赖历史上下文
_ANAPHORA_WORDS = (
    "上述", "上面", "上一个", "上个", "它", "他们", "该指标", "这个指标",
    "继续", "再", "接着", "还有", "也看看",
)


def _bigrams(text: str) -> set:
    return {text[i : i + 2] for i in range(len(text) - 1)}


class RelevanceChecker:
    def __init__(self, get_chat_model: Callable[[], Any], settings: Settings) -> None:
        self._get_chat_model = get_chat_model
        self._settings = settings

    async def check(
        self, recent_rounds: List[RoundData], current_query: str, callbacks=None
    ) -> RelevanceResult:
        if not recent_rounds:
            return RelevanceResult(related=False, rewritten_query=current_query)

        llm = self._get_chat_model()
        if llm is None:
            return self._rule_based(recent_rounds, current_query)

        try:
            history_text = "\n\n".join(r.format() for r in recent_rounds)
            result: RelevanceResult = await astructured_invoke(
                llm,
                RelevanceResult,
                [
                    SystemMessage(content=load_prompt("relevance")["system"]),
                    HumanMessage(
                        content=f"最近的历史问答：\n{history_text}\n\n当前用户输入：{current_query}"
                    ),
                ],
                config=({"callbacks": callbacks} if callbacks else None),
            )
            # 改写结果与原文一致且判定不相关时，倾向保守判定为相关（老系统熵校验精神）
            if not result.related and len(result.rewritten_query) > len(current_query):
                result.related = True
            return result
        except Exception as exc:  # noqa: BLE001 - 判断失败降级规则
            logger.warning("LLM 相关性判断失败，回退规则模式：%s", exc)
            return self._rule_based(recent_rounds, current_query)

    @staticmethod
    def _rule_based(recent_rounds: List[RoundData], current_query: str) -> RelevanceResult:
        stripped = current_query.strip()
        # 1. 指代/追加词命中，或句首指代（"那付费转化率呢"式省略）-> 相关
        if any(w in current_query for w in _ANAPHORA_WORDS) or stripped.startswith("那"):
            return RelevanceResult(related=True, rewritten_query=current_query)
        # 2. 与最近任意一轮 query 的字符 bigram 重叠率 > 0.15 -> 相关
        query_bigrams = _bigrams(current_query)
        for r in recent_rounds:
            hist_bigrams = _bigrams(r.user_query)
            if hist_bigrams and query_bigrams:
                overlap = len(query_bigrams & hist_bigrams) / len(query_bigrams | hist_bigrams)
                if overlap > 0.15:
                    return RelevanceResult(related=True, rewritten_query=current_query)
        return RelevanceResult(related=False, rewritten_query=current_query)
