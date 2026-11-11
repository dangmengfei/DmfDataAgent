"""时间解析模块（Phase 2.2）。

迁移自老系统 plugin/get_indicators/get_time/（三通道：规则 / LLM / SQL）。
v2 采用自包含规则解析器，输出格式对齐老系统 LLM 通道约定
（beginTime/endTime/times/statTime，时间实体 YYYY-MM-DD / YYYY-MM / YYYY / YYYYQX / YYYY-WX）。

- model.py      : 结果模型 ParsedTime（各通道共用）
- format.py     : 时间格式工具 + 中文数字 + 农历换算 + 简繁归一化
- rules.py      : 规则通道（相对/绝对/节日/工作日/节气/农历）—— 2.2a
- festival.py   : 常用公历/农历节日表（lunardate 换算）
- solar_terms.py: 二十四节气（天文算法 + 真实值校正表）
- holidays.py   : 法定节假日/调休日历（交易日历：工作日判定）
- llm.py        : LLM 通道（结构化输出，迁移老 get_time_prompt）—— 2.2b
- 老 SQL 时间通道（time_by_sql）依赖取数链路，Phase 3 一并评估
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

from dmf_agent.time.format import cn_to_int, quarter_format, to_simplified, week_format
from dmf_agent.time.holidays import is_workday, nth_workday_backwards
from dmf_agent.time.llm import parse_with_llm
from dmf_agent.time.model import ParsedTime
from dmf_agent.time.rules import RuleTimeParser, parse_time
from dmf_agent.time.solar_terms import solar_term_date

__all__ = [
    "ParsedTime",
    "RuleTimeParser",
    "cn_to_int",
    "is_workday",
    "nth_workday_backwards",
    "parse_time",
    "parse_time_combined",
    "parse_with_llm",
    "quarter_format",
    "solar_term_date",
    "to_simplified",
    "week_format",
]


async def parse_time_combined(
    query: str,
    *,
    today: Optional[date] = None,
    get_chat_model: Optional[Callable[[], Any]] = None,
    callbacks: Optional[list] = None,
) -> ParsedTime:
    """组合入口：LLM 通道优先（可用时），规则通道兜底。

    extract_elements / 其他消费方统一走此入口。
    """
    parsed = await parse_with_llm(
        query, today=today, get_chat_model=get_chat_model, callbacks=callbacks
    )
    if parsed is not None and not parsed.is_empty:
        return parsed
    return parse_time(query, today=today)
