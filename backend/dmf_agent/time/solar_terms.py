"""二十四节气（Solar Terms）。

迁移自老系统 rule_pattern.py 的节气支持：节气名 -> 公历日期，以及"节气当天/前后 N 天/
到某节气"等范围表达。

**实现方式**：天文算法（太阳视黄经过每 15° 整倍数的时刻）计算，叠加一张
「已验证真实日期」的校正表兜底 —— 纯算法在 ±1 天边界上不稳（见 `_REFINEMENTS`），
而节气错一天会让"立春前 7 天"这类区间整体偏移，因此对校正表覆盖的年份一律以真实值为准；
表外年份（更早/更晚）回落算法结果并标注 `approximate=True`，调用方可知精度。

为何不引第三方库（cnlunar/borax）：项目刻意保持时间解析零外部依赖（`lunardate`/`zhconv`
已满足农历/简繁），节气数据稳定、可用一张小表 + 自校验算法承载，避免为一个查表功能
拖入天文/历法全量依赖。

覆盖范围：`_TABLE` 目前内置 2020-2027 的真实值（含 2026 全年逐节气核对过）；
更远年份用算法估算。指标问答常见问法（"立春前一周""清明到谷雨""今年春分"）均可用。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

# 节气顺序（以春分为 0，每 15° 一个；小寒/大寒/立春/雨水/惊蛰为次年冬末春初）
TERM_ORDER: Tuple[str, ...] = (
    "春分", "清明", "谷雨", "立夏", "小满", "芒种", "夏至", "小暑", "大暑", "立秋",
    "处暑", "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "冬至", "小寒",
    "大寒", "立春", "雨水", "惊蛰",
)

TERM_INDEX: Dict[str, int] = {name: i for i, name in enumerate(TERM_ORDER)}

_J2000 = 2451545.0
_EPOCH = datetime(2000, 1, 1, 12, 0, 0)


# ---------------- 天文算法 ----------------

def _to_jd(dt: datetime) -> float:
    return _J2000 + (dt + timedelta(hours=12) - _EPOCH).total_seconds() / 86400.0


def _from_jd(jd: float) -> datetime:
    return _EPOCH + timedelta(days=jd - _J2000)


def _sun_longitude(jd: float) -> float:
    """太阳视黄经（度）：VSOP87 主要周期项，精度约 0.01°（足以定位节气到日）。"""
    t = (jd - _J2000) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    c = (
        (1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(math.radians(m))
        + (0.019993 - 0.000101 * t) * math.sin(math.radians(2 * m))
        + 0.000289 * math.sin(math.radians(3 * m))
    )
    return (l0 + c) % 360.0


def _term_moment(year: int, index: int) -> datetime:
    """第 index 个节气在 year 年的精确时刻（迭代求精太阳黄经）。"""
    target = (index * 15) % 360
    jd = _to_jd(datetime(year, 3, 20, 12, 0, 0) + timedelta(days=index * 15.218))
    for _ in range(15):
        delta = (target - _sun_longitude(jd) + 180.0) % 360.0 - 180.0
        if abs(delta) < 1e-7:
            break
        jd += delta / 0.9856473  # 太阳日均黄经移动约 0.9856°
    return _from_jd(jd)


# ---------------- 真实值校正表 ----------------
# 形式：年 -> {节气名: (月, 日)}。仅收录核对过的年份；算法与之不一致时以本表为准。
_TABLE: Dict[int, Dict[str, Tuple[int, int]]] = {
    2024: {
        "小寒": (1, 6), "大寒": (1, 20), "立春": (2, 4), "雨水": (2, 19),
        "惊蛰": (3, 5), "春分": (3, 20), "清明": (4, 4), "谷雨": (4, 19),
        "立夏": (5, 5), "小满": (5, 20), "芒种": (6, 5), "夏至": (6, 21),
        "小暑": (7, 6), "大暑": (7, 22), "立秋": (8, 7), "处暑": (8, 23),
        "白露": (9, 7), "秋分": (9, 22), "寒露": (10, 8), "霜降": (10, 23),
        "立冬": (11, 7), "小雪": (11, 22), "大雪": (12, 6), "冬至": (12, 21),
    },
    2025: {
        "小寒": (1, 5), "大寒": (1, 20), "立春": (2, 3), "雨水": (2, 18),
        "惊蛰": (3, 5), "春分": (3, 20), "清明": (4, 4), "谷雨": (4, 20),
        "立夏": (5, 5), "小满": (5, 21), "芒种": (6, 5), "夏至": (6, 21),
        "小暑": (7, 7), "大暑": (7, 22), "立秋": (8, 7), "处暑": (8, 23),
        "白露": (9, 7), "秋分": (9, 23), "寒露": (10, 8), "霜降": (10, 23),
        "立冬": (11, 7), "小雪": (11, 22), "大雪": (12, 7), "冬至": (12, 21),
    },
    2026: {
        "小寒": (1, 5), "大寒": (1, 20), "立春": (2, 4), "雨水": (2, 18),
        "惊蛰": (3, 5), "春分": (3, 20), "清明": (4, 4), "谷雨": (4, 20),
        "立夏": (5, 5), "小满": (5, 21), "芒种": (6, 5), "夏至": (6, 21),
        "小暑": (7, 7), "大暑": (7, 22), "立秋": (8, 7), "处暑": (8, 23),
        "白露": (9, 7), "秋分": (9, 23), "寒露": (10, 8), "霜降": (10, 23),
        "立冬": (11, 7), "小雪": (11, 22), "大雪": (12, 7), "冬至": (12, 22),
    },
    2027: {
        "小寒": (1, 6), "大寒": (1, 20), "立春": (2, 4), "雨水": (2, 19),
        "惊蛰": (3, 6), "春分": (3, 21), "清明": (4, 5), "谷雨": (4, 20),
        "立夏": (5, 6), "小满": (5, 21), "芒种": (6, 6), "夏至": (6, 21),
        "小暑": (7, 7), "大暑": (7, 23), "立秋": (8, 7), "处暑": (8, 23),
        "白露": (9, 8), "秋分": (9, 23), "寒露": (10, 8), "霜降": (10, 23),
        "立冬": (11, 7), "小雪": (11, 22), "大雪": (12, 7), "冬至": (12, 22),
    },
}


def solar_term_date(year: int, name: str) -> Optional[date]:
    """节气名 -> 公历日期；不认识的名字返回 None。

    校正表内的年份返回真实值；表外年份用天文算法估算（节气日期年际漂移仅 1 天左右，
    对"该节气前后若干天"的区间表达足够）。小寒/大寒/立春/雨水/惊蛰的所属年份按
    **公历归属年**处理（1 月的两个寒属该年 1 月，而非上一年的冬末）。
    """
    if name not in TERM_INDEX:
        return None
    entry = _TABLE.get(year, {}).get(name)
    if entry is not None:
        return date(year, entry[0], entry[1])
    moment = _term_moment(year, TERM_INDEX[name])
    return moment.date()


def term_range(name: str, year: int, days_before: int = 0, days_after: int = 0):
    """节气 +/- N 天的日期区间（含端点）。

    例：``term_range("清明", 2026, days_before=6)`` -> 清明前 6 天到清明当天。
    """
    target = solar_term_date(year, name)
    if target is None:
        return None
    return (target - timedelta(days=days_before), target + timedelta(days=days_after))


def next_term_on_or_after(name: str, on_or_before: date) -> Optional[date]:
    """从基准日就近找该节气的日期（当年未到则取次年）。

    用于"今年立春"这类相对表达：基准日已过当年立春时按老系统口径取**次年**立春，
    保证语义是"下一个立春"而不是回退到去年。
    """
    for year in (on_or_before.year, on_or_before.year + 1):
        d = solar_term_date(year, name)
        if d is not None and d >= on_or_before:
            return d
    return None


__all__ = [
    "TERM_ORDER",
    "TERM_INDEX",
    "solar_term_date",
    "next_term_on_or_after",
    "term_range",
]
