"""Token 消耗统计回调（LangChain）。

在 ChatService 的一轮对话 `graph.astream` 配置中挂载本回调，
即可在一次图执行内累计所有 LLM 调用的 token 用量（prompt / completion / total），
与具体模型厂商无关：优先读 OpenAI 兼容的 `llm_output.token_usage`，
回退到 AIMessage 的 `usage_metadata`（LangChain 0.3+）。
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TokenUsageCallback(BaseCallbackHandler):
    """累计一次对话中所有 LLM 调用的 token 用量。"""

    def __init__(self) -> None:
        super().__init__()
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.call_count: int = 0

    def _accumulate(self, usage: Optional[dict]) -> None:
        if not usage:
            return
        self.prompt_tokens += _coerce_int(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        )
        self.completion_tokens += _coerce_int(
            usage.get("completion_tokens") or usage.get("output_tokens")
        )
        self.total_tokens += _coerce_int(
            usage.get("total_tokens")
            or (self.prompt_tokens + self.completion_tokens)
        )
        self.call_count += 1

    def on_llm_end(self, response, *, run_id: Any = None, parent_run_id: Any = None, **kwargs: Any) -> None:
        # OpenAI 兼容：response.llm_output = {"token_usage": {...}, "model_name": ...}
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            self._accumulate(llm_output.get("token_usage") or llm_output.get("usage"))
            if self.call_count:
                return

        # 回退：从 generations 里取 message.usage_metadata
        for gen_list in getattr(response, "generations", []) or []:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                um = getattr(msg, "usage_metadata", None)
                if isinstance(um, dict):
                    self._accumulate(
                        {
                            "prompt_tokens": um.get("input_tokens"),
                            "completion_tokens": um.get("output_tokens"),
                            "total_tokens": um.get("total_tokens"),
                        }
                    )
                    if self.call_count:
                        return
