"""常用节日解析（公历固定 + 农历节日）。

迁移自老系统 rule_pattern.py 的节日覆盖范围（FIXED_SOLAR_FESTIVAL /
FIXED_LUNAR_FESTIVAL），取指标业务常见子集：
- 固定公历节日：元旦/妇女节/植树节/315/劳动节/青年节/儿童节/建党/建军/教师节/
  国庆/圣诞/双11/情人节/愚人节/万圣节
- 农历节日（经 lunardate 换算公历）：春节/元宵/端午/七夕/中秋/重阳/腊八/除夕/小年
- 规律节日：母亲节（5月第2个周日）/父亲节（6月第3个周日）/感恩节（11月第4个周四）

春节与国庆按假期给出范围（春节：除夕~大年初六；国庆：10-01~10-07），
其余节日解析为当天点。农历换算依赖 lunardate（纯 Python）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional, Tuple

from lunardate import LunarDate

from dmf_agent.time.format import to_ymd
from dmf_agent.time.model import ParsedTime

# 固定公历节日：名称别名 -> (月, 日)
_FIXED_SOLAR: Dict[str, Tuple[int, int]] = {
    "元旦": (1, 1),
    "妇女节": (3, 8), "女神节": (3, 8), "三八": (3, 8),
    "植树节": (3, 12),
    "消费者权益日": (3, 15),
    "劳动节": (5, 1), "五一": (5, 1),
    "青年节": (5, 4),
    "儿童节": (6, 1), "六一": (6, 1),
    "建党节": (7, 1), "七一": (7, 1),
    "建军节": (8, 1), "八一": (8, 1),
    "教师节": (9, 10),
    "国庆": (10, 1), "十一": (10, 1),
    "圣诞": (12, 25),
    "双十一": (11, 11),
    "情人节": (2, 14),
    "愚人节": (4, 1),
    "万圣节": (10, 31),
}

# 农历节日：名称别名 -> (农历月, 农历日)
_LUNAR: Dict[str, Tuple[int, int]] = {
    "除夕": (12, 30),  # 腊月三十（小月时 lunardate 自动取廿九）
    "春节": (1, 1),
    "元宵": (1, 15),
    "端午": (5, 5),
    "七夕": (7, 7),
    "中秋": (8, 15),
    "重阳": (9, 9),
    "腊八": (12, 8),
    "小年": (12, 23),
}

# 春节/国庆按假期范围（相对基准日的天数偏移：(起, 止)）
# 春节基准=正月初一：除夕(-1) ~ 初六(+5)；国庆基准=10月1日：当天(0) ~ 10月7日(+6)
_HOLIDAY_RANGES: Dict[str, Tuple[int, int]] = {
    "春节": (-1, 5),
    "国庆": (0, 6),
}


def _lunar_to_date(year: int, month: int, day: int) -> date:
    """农历 -> 公历（lunardate；除夕若为小月自动取腊月廿九）。"""
    return LunarDate(year, month, day).to_solar_date()


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """某月第 n 个指定星期几（0=周一）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (nth - 1) * 7)


def resolve_festival(query: str, year: Optional[int] = None) -> Optional[ParsedTime]:
    """在 query 中查找节日表达，解析为时间范围/点；未命中返回 None。"""
    if not query:
        return None
    year = year or date.today().year
    aliases: Dict[str, str] = {}
    for name in _FIXED_SOLAR:
        aliases[name] = "solar"
    for name in _LUNAR:
        aliases[name] = "lunar"

    # 最长别名优先匹配
    for name in sorted(aliases, key=len, reverse=True):
        if name not in query:
            continue
        kind = aliases[name]
        if kind == "solar":
            month, day = _FIXED_SOLAR[name]
            target = date(year, month, day)
        else:
            month, day = _LUNAR[name]
            target = _lunar_to_date(year, month, day)
        return _holiday_time(name, target)

    # 规律节日（当年无则顺延次年）
    regular = _match_regular(query, year)
    if regular is not None:
        return _holiday_time("regular", regular)
    return None


def _match_regular(query: str, year: int) -> Optional[date]:
    if "母亲节" in query:
        return _nth_weekday(year, 5, 6, 2)  # 5 月第 2 个周日
    if "父亲节" in query:
        return _nth_weekday(year, 6, 6, 3)  # 6 月第 3 个周日
    if "感恩节" in query:
        return _nth_weekday(year, 11, 3, 4)  # 11 月第 4 个周四
    return None


def _holiday_time(name: str, target: date) -> ParsedTime:
    offsets = _HOLIDAY_RANGES.get(name)
    if offsets:
        begin, end = offsets
        return ParsedTime(
            begin_time=to_ymd(target + timedelta(days=begin)),
            end_time=to_ymd(target + timedelta(days=end)),
            granularity="day",
            raw=name,
        )
    return ParsedTime(times=[to_ymd(target)], granularity="day", raw=name)
