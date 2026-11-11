"""时间格式工具。

对齐老系统 get_time 的输出格式约定（get_time_prompt 输出要求）：
- YYYY-MM-DD 年月日 / YYYY-MM 年月 / YYYY 年 / YYYYQX 年季（如 2024Q2）/ YYYY-WX 年周（如 2024-W3）
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

try:  # lunardate 为纯 Python 依赖，缺失时农历解析降级为未识别（不抛错）
    from lunardate import LunarDate
except ImportError:  # pragma: no cover
    LunarDate = None  # type: ignore[assignment]

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100,
    "千": 1000, "万": 10000, "亿": 100000000,
}


def to_simplified(text: str) -> str:
    """繁体 -> 简体归一化（zhconv；未安装时原样返回，保持降级可用）。

    老系统无简繁处理（正则仅覆盖简体），本项为 v2 增强：
    在时间解析入口归一化，保证"近5個工作日"等繁体输入可被规则命中。
    """
    try:
        from zhconv import convert
    except ImportError:  # pragma: no cover
        return text
    return convert(text, "zh-cn")


def cn_to_int(s: str) -> int | None:
    """中文数字转阿拉伯数字（支持 俩/两/仨/十百千万/亿/零）。

    迁移自老系统 DELTA_NUM_STRING 支持的写法。解析失败返回 None。
    例：十五->15，三十->30，二百五->250，两万三千五百六十七->23567，一千零五->1005。
    """
    s = s.strip()
    if not s or not re.fullmatch(r"[零〇一二三四五六七八九两俩十百千万亿]+", s):
        return None
    total = 0  # 已累加的大段
    section = 0  # 当前段内累计
    number = 0  # 当前位数字
    for ch in s:
        val = _CN_DIGITS[ch]
        if val < 10:
            number = val
        elif val == 10:
            section += (number or 1) * val
            number = 0
        elif val < 10000:  # 百/千
            section += (number or 1) * val
            number = 0
        else:  # 万/亿
            section += number
            total += (section or 1) * val
            section = 0
            number = 0
    total += section + number
    return total


def to_ymd(d: date) -> str:
    """YYYY-MM-DD。"""
    return d.isoformat()


def to_ym(d: date) -> str:
    """YYYY-MM。"""
    return f"{d.year:04d}-{d.month:02d}"


def quarter_format(d: date) -> str:
    """YYYYQX（X 为季度序号）。"""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def week_format(d: date) -> str:
    """YYYY-WX（X 为 ISO 周序号）。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]}"


def first_day_of_month(d: date) -> date:
    return d.replace(day=1)


def last_day_of_month(d: date) -> date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1) - timedelta(days=1)
    return d.replace(month=d.month + 1, day=1) - timedelta(days=1)


def first_day_of_quarter(d: date) -> date:
    """季首（1/4/7/10 月 1 日）。"""
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def last_day_of_quarter(d: date) -> date:
    """季末（3/6/9/12 月最后一天）。"""
    q_end_month = ((d.month - 1) // 3) * 3 + 3
    return last_day_of_month(date(d.year, q_end_month, 1))


def first_day_of_year(d: date) -> date:
    return date(d.year, 1, 1)


def last_day_of_year(d: date) -> date:
    return date(d.year, 12, 31)


def shift_year(d: date, n: int) -> date:
    """平移 n 年（2 月 29 日平年时退回 2 月 28 日）。"""
    year = d.year + n
    day = d.day
    if d.month == 2 and d.day == 29 and not _is_leap(year):
        day = 28
    return date(year, d.month, day)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def lunar_to_solar(
    year: int, month: int, day: int, leap: bool = False
) -> Optional[date]:
    """农历日期 -> 公历（lunardate；依赖未安装/越界返回 None）。

    ``leap=True`` 表示该月为闰月（lunardate 以第 4 个位置参数表达，**不是** month+12——
    那样会撞上"13 月"这种非法月号）。除夕（腊月三十）遇小月时由 lunardate 取腊月廿九。
    """
    if LunarDate is None:
        return None
    try:
        return LunarDate(year, month, day, leap).to_solar_date()
    except Exception:  # noqa: BLE001 - 越界农历日期（如闰月不存在）按未识别处理
        return None


def resolve_lunar(year: int, month: int, day: int, leap: bool = False) -> Optional[date]:
    """农历(年,月,日) -> 公历，leap=True 表示该月为闰月。

    集中在此处而非各调用点散判，保证"腊月三十在小月取廿九"与闰月语义一致。
    """
    if not (1 <= month <= 12):
        return None
    return lunar_to_solar(year, month, day, leap=leap)


def shift_month(d: date, n: int) -> date:
    """把日期平移 n 个月（近似：取目标月第 day 日，超出则取月末）。"""
    month_index = d.year * 12 + (d.month - 1) + n
    year, month = divmod(month_index, 12)
    month += 1
    last = last_day_of_month(date(year, month, 1))
    return date(year, month, min(d.day, last.day))
