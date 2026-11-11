"""LLM 时间通道。

迁移自老系统 get_time_llm.py::TimeParserLLM（生产主路径）：
- 输出结构对齐老 get_time_prompt：bt/et（连续范围）、t（离散节点）、g（粒度）、thoughts
- 时间实体格式 YYYY-MM-DD / YYYY-MM / YYYY / YYYYQX / YYYY-WX
- 注入"今天是{today},{week_day}"（老 prompt 同款），并给出生动示例语义
- LLM 不可用 / 结构化输出失败时返回 None，由组合入口回退规则通道

老系统的动态示例召回（DynamicExamplePrompt 经验库）由 experience.store.ExampleStore 承接（本地 BM25）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from dmf_agent.agents.prompt_loader import load_prompt
from dmf_agent.llm.structured import astructured_invoke
from dmf_agent.time.model import ParsedTime

logger = logging.getLogger(__name__)

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class TimeLLMResult(BaseModel):
    """老系统 get_time_prompt 输出结构。"""

    bt: str = Field("", description="连续时间范围开始，如 2026-06-01；无则空")
    et: str = Field("", description="连续时间范围结束；无则空")
    t: List[str] = Field(default_factory=list, description="离散时间节点")
    g: str = Field("", description="时间粒度：year/quarter/month/week/day")
    thoughts: str = ""


async def parse_with_llm(
    query: str,
    *,
    today: Optional[date] = None,
    get_chat_model: Optional[Callable[[], Any]] = None,
    callbacks: Optional[list] = None,
) -> Optional[ParsedTime]:
    """LLM 通道解析；LLM 不可用 / 调用失败返回 None（上层回退规则通道）。"""
    if get_chat_model is None:
        return None
    llm = get_chat_model()
    if llm is None:
        return None
    today = today or date.today()
    try:
        system = load_prompt("time_llm")["system"].format(
            today=today.isoformat(),
            week_day=_WEEKDAY_CN[today.weekday()],
        )
        kwargs = {"config": {"callbacks": callbacks}} if callbacks else {}
        result: TimeLLMResult = await astructured_invoke(
            llm,
            TimeLLMResult,
            [SystemMessage(content=system), HumanMessage(content=query)],
            **kwargs,
        )
        parsed = _to_parsed_time(result, today)
        if parsed.is_empty:
            logger.info("LLM 时间解析未识别到时间：%s", query)
            return None
        logger.info("LLM 时间解析：%s -> %s", query, parsed)
        return parsed
    except Exception as exc:  # noqa: BLE001 - 回退规则通道
        logger.warning("LLM 时间解析失败，回退规则通道：%s", exc)
        return None


def _to_parsed_time(result: TimeLLMResult, today: date) -> ParsedTime:
    g = result.g.strip()
    # 老系统兼容：g 含逗号多值取最后一个
    if "," in g:
        g = g.split(",")[-1].strip()
    if result.bt and result.et:
        return ParsedTime(
            begin_time=result.bt.strip(),
            end_time=result.et.strip(),
            granularity=g,
        )
    times = [t.strip() for t in result.t if t.strip()]
    if times:
        return ParsedTime(times=times, granularity=g)
    return ParsedTime()
