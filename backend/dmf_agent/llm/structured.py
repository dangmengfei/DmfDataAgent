"""结构化输出辅助器（带多级降级）。

背景：LangChain 的 ``llm.with_structured_output(Schema)`` 默认走 OpenAI 的
``response_format={"type":"json_schema"}``。部分模型（典型如推理模型
deepseek-reasoner / deepseek-v4-flash 一类）**不支持该 response_format**，会直接
返回 400 ``This response_format type is unavailable now``，导致节点静默回退 mock，
表现为"配了真实 LLM 但链路仍走模板"。

本模块提供 :func:`astructured_invoke`，按下列顺序逐级尝试，任一成功即返回：

1. ``json_schema``（原生结构化输出，能力最全的模型走这条）
2. ``function_calling``（工具调用方式）
3. ``json_mode``（``response_format={"type":"json_object"}``）
4. **提示词兜底**：把 JSON Schema 写进指令，要求模型只输出 JSON，再从文本里
   抽取并校验（对不支持任何 response_format 的推理模型也适用）

全部失败才抛 :class:`StructuredOutputError`，交由上层决定降级策略（而不是在
LLM 层静默 mock）。所有通道的失败原因会汇总在异常信息里，便于定位模型能力差异。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Sequence, Type, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ```json ... ``` 代码块
_FENCED_JSON = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.S)


class StructuredOutputError(RuntimeError):
    """所有结构化输出通道均失败。"""


def _extract_json(text: str) -> Any:
    """从模型输出中抽取 JSON（容忍 ```json 代码块与前后废话）。"""
    raw = (text or "").strip()
    m = _FENCED_JSON.search(raw)
    if m:
        raw = m.group(1)
    else:
        start = min((i for i in (raw.find("{"), raw.find("[")) if i >= 0), default=-1)
        end = max(raw.rfind("}"), raw.rfind("]"))
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _coerce(schema: Type[T], out: Any) -> T:
    """把通道返回值规整为 schema 实例（部分实现返回 dict）。"""
    if isinstance(out, schema):
        return out
    if isinstance(out, dict):
        return schema.model_validate(out)
    raise TypeError(f"结构化输出类型异常：{type(out).__name__}")


async def astructured_invoke(
    llm: Any,
    schema: Type[T],
    messages: Sequence[BaseMessage],
    *,
    config: Optional[dict] = None,
    json_hint: Optional[str] = None,
) -> T:
    """带降级的结构化调用；返回 ``schema`` 实例。

    :param llm: LangChain ChatModel
    :param schema: pydantic 模型类
    :param messages: 已构造好的消息序列
    :param config: 透传给 ainvoke 的 config（如 callbacks，用于 token 观测）
    :param json_hint: 覆盖提示词兜底阶段的指令（默认按 schema 自动生成）
    """
    kwargs: dict = {"config": config} if config else {}
    errors: list[str] = []

    for method in (None, "function_calling", "json_mode"):
        try:
            structured = (
                llm.with_structured_output(schema)
                if method is None
                else llm.with_structured_output(schema, method=method)
            )
            return _coerce(schema, await structured.ainvoke(list(messages), **kwargs))
        except Exception as exc:  # noqa: BLE001 - 逐级降级
            errors.append(f"{method or 'json_schema'}: {exc}")
            logger.debug("结构化输出通道 %s 失败：%s", method or "json_schema", exc)

    # 提示词兜底：不依赖任何 response_format，对推理模型同样有效
    try:
        instruction = json_hint or (
            "请只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块，"
            "必须严格满足以下 JSON Schema：\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        resp = await llm.ainvoke([*messages, HumanMessage(content=instruction)], **kwargs)
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        return schema.model_validate(_extract_json(content))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"prompt_json: {exc}")

    raise StructuredOutputError("结构化输出全部通道失败：" + " | ".join(errors))
