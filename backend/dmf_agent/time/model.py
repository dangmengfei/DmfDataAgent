"""时间解析结果模型与构造工具（供 rules / festival / llm 各通道共用）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional

from dmf_agent.time.format import to_ymd


@dataclass
class ParsedTime:
    """时间解析结果（对齐老系统 beginTime/endTime/times/statTime）。"""

    begin_time: Optional[str] = None  # YYYY-MM-DD
    end_time: Optional[str] = None  # YYYY-MM-DD
    times: List[str] = field(default_factory=list)  # 离散节点
    granularity: str = ""  # day | week | month | quarter | year
    raw: str = ""  # 命中的原文片段

    @property
    def is_empty(self) -> bool:
        return not (self.begin_time or self.end_time or self.times)

    def to_elements(self) -> Optional[dict]:
        """转为要素抽取的 time_range 结构（start/end/granularity + 离散点 times）。

        离散节点（如节日/多个月份）除 times 字段原样透传外，
        用首尾节点填充 start/end，保证下游范围字段可用。
        """
        if self.is_empty:
            return None
        begin = self.begin_time or (self.times[0] if self.times else None)
        end = self.end_time or (self.times[-1] if self.times else None)
        result: dict = {
            "start": begin,
            "end": end,
            "granularity": self.granularity or None,
        }
        if self.times:
            result["times"] = list(self.times)
        return result


def range_time(begin: date, end: date, granularity: str) -> ParsedTime:
    return ParsedTime(
        begin_time=to_ymd(begin), end_time=to_ymd(end), granularity=granularity
    )


def point_time(d: date, granularity: str, fmt: Callable[[date], str] = to_ymd) -> ParsedTime:
    return ParsedTime(times=[fmt(d)], granularity=granularity)
