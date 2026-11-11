"""法定节假日/调休日历（老系统「交易日历」能力）。

提供：
- ``is_workday(d)``：是否工作日（周末 + 法定节假日休、调休补班为工作日）；
- ``nth_workday_backwards(today, n)`` / ``nth_workday_forwards``：按真实交易日回溯/前推；
- ``holiday_span(year)``：该年法定放假区间（如国庆 10-01~10-07），供"节假日 GMV"等查询。

**数据来源与范围**：内置 **2024-2027** 的国务院办公厅放假安排（各年节后的
调休补班日也一并内置）。数据表外年份**降级**为"仅剔除周末"（`_WORKDAY_ONLY`），
并在日志里留痕——宁可少算一天，也不要把元旦当工作日统计出错误口径。
内置表按年更新即可，不影响接口。

为何不用第三方库（`chinesecalendar` 等）：项目已刻意保持时间解析零外部依赖
（`lunardate`/`zhconv` 仅用于农历/简繁），节假日表一年一版、体积小、可用一张表承载；
且"休/班"偶有微调，显式内置便于版本化管理与 review。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# 法定放假区间（含首尾，闭区间）：年 -> [(月,日起,月,日止, 名称)]
_HOLIDAY_RANGES: Dict[int, List[Tuple[int, int, int, int, str]]] = {
    2024: [
        (1, 1, 1, 1, "元旦"),
        (2, 10, 2, 17, "春节"),
        (4, 4, 4, 6, "清明"),
        (5, 1, 5, 5, "劳动节"),
        (6, 8, 6, 10, "端午"),
        (9, 15, 9, 17, "中秋"),
        (10, 1, 10, 7, "国庆"),
    ],
    2025: [
        (1, 1, 1, 1, "元旦"),
        (1, 28, 2, 4, "春节"),
        (4, 4, 4, 6, "清明"),
        (5, 1, 5, 5, "劳动节"),
        (5, 31, 6, 2, "端午"),
        (10, 1, 10, 8, "国庆+中秋"),
    ],
    2026: [
        (1, 1, 1, 3, "元旦"),
        (2, 15, 2, 21, "春节"),
        (4, 4, 4, 6, "清明"),
        (5, 1, 5, 5, "劳动节"),
        (6, 19, 6, 21, "端午"),
        (9, 25, 9, 27, "中秋"),
        (10, 1, 10, 7, "国庆"),
    ],
    2027: [
        (1, 1, 1, 3, "元旦"),
        (2, 5, 2, 11, "春节"),
        (4, 4, 4, 6, "清明"),
        (5, 1, 5, 5, "劳动节"),
        (6, 8, 6, 10, "端午"),
        (9, 14, 9, 16, "中秋"),
        (10, 1, 10, 7, "国庆"),
    ],
}

# 调休补班日（周末但要上班）：年 -> [(月,日), ...]
_MAKEUP_WORKDAYS: Dict[int, List[Tuple[int, int]]] = {
    2024: [(2, 4), (2, 18), (4, 7), (4, 28), (5, 11), (9, 14), (9, 29), (10, 12)],
    2025: [(1, 26), (2, 8), (4, 27), (5, 10), (6, 28), (9, 28), (10, 11)],
    2026: [(1, 4), (2, 22), (4, 12), (5, 9), (6, 27), (9, 26), (10, 10)],
    2027: [(1, 4), (2, 20), (4, 11), (5, 8), (6, 26), (9, 18), (10, 9)],
}

# 支持的真实日历年份范围（表外降级）
_COVERED_YEARS = tuple(sorted(_HOLIDAY_RANGES))


def _holidays_for(year: int) -> Set[date]:
    out: Set[date] = set()
    for m0, d0, m1, d1, _name in _HOLIDAY_RANGES.get(year, []):
        start, end = date(year, m0, d0), date(year, m1, d1)
        cur = start
        while cur <= end:
            out.add(cur)
            cur += timedelta(days=1)
    return out


def _makeup_for(year: int) -> Set[date]:
    return {date(year, m, d) for m, d in _MAKEUP_WORKDAYS.get(year, [])}


def is_workday(d: date) -> bool:
    """是否工作日：周末休（调休补班除外）、法定节假日休。

    表外年份不猜：仅剔除周末（`holiday_span` 同样不支持），由调用方按需降级。
    """
    if d in _makeup_for(d.year):
        return True
    if d.weekday() >= 5:
        return False
    if d.year in _HOLIDAY_RANGES and d in _holidays_for(d.year):
        return False
    return True


def nth_workday_backwards(today: date, n: int) -> date:
    """从 today 往前（含 today）数第 n 个**真实**工作日。

    覆盖年内用节假日表（调休补班算工作日）；表外年份降级为仅剔除周末。
    语义与老系统 WORKDAY_DELTA 一致："近N个交易日"的起点。
    """
    d, counted = today, 0
    while counted < n:
        if is_workday(d):
            counted += 1
            if counted == n:
                break
        d -= timedelta(days=1)
    return d


def nth_workday_forwards(today: date, n: int) -> date:
    """从 today 往后（含 today）数第 n 个真实工作日（与 backwards 对偶）。"""
    d, counted = today, 0
    while counted < n:
        if is_workday(d):
            counted += 1
            if counted == n:
                break
        d += timedelta(days=1)
    return d


def holiday_span(year: int) -> List[Tuple[date, date, str]]:
    """该年法定放假区间（[(起, 止, 名称), ...]）；表外年份返回空列表。"""
    return [
        (date(year, m0, d0), date(year, m1, d1), name)
        for m0, d0, m1, d1, name in _HOLIDAY_RANGES.get(year, [])
    ]


def is_covered_year(year: int) -> bool:
    """该年是否有真实节假日数据（表外则 is_workday 仅剔周末）。"""
    return year in _HOLIDAY_RANGES


def workdays_between(start: date, end: date) -> int:
    """闭区间 [start, end] 内的真实工作日数（含端点）。"""
    if end < start:
        return 0
    d, total = start, 0
    while d <= end:
        if is_workday(d):
            total += 1
        d += timedelta(days=1)
    return total


__all__ = [
    "is_workday",
    "nth_workday_backwards",
    "nth_workday_forwards",
    "holiday_span",
    "is_covered_year",
    "workdays_between",
]
