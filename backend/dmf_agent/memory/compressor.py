"""轮次滚动压缩器。

迁移自老系统 memory/compression.py::RoundCompressor：
- 老系统维护 previous_summary（滚动摘要），把超出保留窗口的轮次增量压缩进摘要；
- 压缩失败时保留原轮次不丢数据（老系统 compress 失败返回 previous_summary 的语义）；
- LLM 不可用 / 调用失败时降级为模板摘要（截取每轮问答要点），保证记忆链路可用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class RoundData:
    """一轮对话（用户消息 + 助手答复，答复可能缺失）。"""

    user_query: str
    assistant_answer: str = ""
    last_message_id: str = ""

    def format(self, answer_max_chars: int = 300) -> str:
        answer = self.assistant_answer[:answer_max_chars]
        if self.assistant_answer and len(self.assistant_answer) > answer_max_chars:
            answer += "…"
        return f"用户：{self.user_query}\n助手：{answer}" if answer else f"用户：{self.user_query}"


class RoundCompressor:
    def __init__(
        self,
        get_chat_model: Callable[[], Any],
        settings: Settings,
    ) -> None:
        self._get_chat_model = get_chat_model
        self._settings = settings

    async def summarize(
        self,
        previous_summary: str,
        rounds: List[RoundData],
        callbacks: Optional[list] = None,
    ) -> str:
        """把 rounds 增量压缩进 previous_summary，返回新摘要；失败返回空串（调用方放弃本次压缩）。"""
        if not rounds:
            return previous_summary
        llm = self._get_chat_model()
        if llm is None:
            return self._template_summary(previous_summary, rounds)

        try:
            history_text = "\n\n".join(r.format() for r in rounds)
            system = load_prompt("compression")["system"]
            user = (
                f"已有摘要：\n{previous_summary or '（无）'}\n\n"
                f"新增对话轮次：\n{history_text}\n\n请输出融合后的最新摘要。"
            )
            kwargs = {"config": {"callbacks": callbacks}} if callbacks else {}
            resp = await llm.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)], **kwargs
            )
            content = resp.content if isinstance(resp.content, str) else str(resp.content)
            if content.strip():
                return content.strip()
            logger.warning("压缩摘要返回空内容，放弃本次压缩")
            return ""
        except Exception as exc:  # noqa: BLE001 - 压缩失败不丢轮次
            logger.warning("LLM 压缩摘要失败：%s", exc)
            return ""

    @staticmethod
    def _template_summary(previous_summary: str, rounds: List[RoundData]) -> str:
        """mock 模式模板摘要：每轮取用户问题与助手答复首行。"""
        lines = []
        for r in rounds:
            first_line = r.assistant_answer.strip().splitlines()[0] if r.assistant_answer.strip() else ""
            # 剥离 markdown 引用块等装饰
            first_line = first_line.lstrip("> #* ")
            lines.append(f"- 用户问：{r.user_query}；助手答：{first_line[:80]}")
        new_part = "\n".join(lines)
        if previous_summary:
            return f"{previous_summary}\n{new_part}"
        return new_part
