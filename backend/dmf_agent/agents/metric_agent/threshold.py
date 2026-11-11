"""指标级阈值过滤解析（FR1 要素增强）。

把"GMV大于100万 / 销售额超过5000 / 不良贷款率低于1%"这类对**聚合指标**的比较
条件解析为结构化阈值，下推到语义编译器已有的 metric filter（外层 WHERE 作用于
聚合列，等价 HAVING）。区别于维度过滤（filters：渠道='线上'）。

支持：
- 前缀算子：大于/高于/超过/多于(>)、小于/低于/少于(<)、大于等于/不低于/不少于/至少(>=)、
  小于等于/不高于/至多(<=)、等于(=)，以及符号 > < >= <=；
- 后缀：N以上(>=) / N以下(<=)；
- 数值单位：万（×1e4）、亿（×1e8），支持小数。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# 前缀算子（长词优先匹配，避免"大于等于"被"大于"截断）
_PREFIX_OPS = [
    ("大于等于", ">="), ("不低于", ">="), ("不少于", ">="), ("不小于", ">="),
    ("至少", ">="), ("高于等于", ">="), (">=", ">="), ("≥", ">="),
    ("小于等于", "<="), ("不高于", "<="), ("不多于", "<="), ("不大于", "<="),
    ("至多", "<="), ("低于等于", "<="), ("<=", "<="), ("≤", "<="),
    ("大于", ">"), ("高于", ">"), ("超过", ">"), ("超出", ">"), ("多于", ">"), (">", ">"),
    ("小于", "<"), ("低于", "<"), ("少于", "<"), ("<", "<"),
    ("等于", "="), ("=", "="), ("＝", "="),
]

_NUM = r"(\d+(?:\.\d+)?)\s*(亿|万)?"
# 后缀比较：100万以上 / 5000以下
_SUFFIX_RE = re.compile(_NUM + r"\s*(以上|以下)")
_ALLOWED_OPS = {">", "<", ">=", "<=", "=", "==", "!="}


def _scale_number(num: str, unit: Optional[str]) -> float:
    value = float(num)
    if unit == "万":
        value *= 1e4
    elif unit == "亿":
        value *= 1e8
    return value


def _metric_positions(text: str, metrics: List[str]) -> Dict[str, int]:
    """各指标在问句中最早出现的位置。"""
    positions: Dict[str, int] = {}
    for name in metrics:
        if not name:
            continue
        pos = text.find(name)
        if pos >= 0:
            positions[name] = pos
    return positions


def _nearest_metric(text_pos: int, positions: Dict[str, int]) -> Optional[str]:
    """选择离条件位置最近的指标；唯一指标直接采用。"""
    if not positions:
        return None
    if len(positions) == 1:
        return next(iter(positions))
    return min(positions, key=lambda name: abs(positions[name] - text_pos))


def parse_metric_filters(text: str, metrics: List[str]) -> List[Dict[str, object]]:
    """解析指标阈值条件 -> [{metric, operator, value}]；无法归属到指标则丢弃。"""
    if not text or not metrics:
        return []
    positions = _metric_positions(text, list(metrics))
    if not positions:
        return []

    found: List[tuple[int, str, float]] = []  # (条件位置, 算子, 数值)
    for word, operator in _PREFIX_OPS:
        pattern = re.compile(re.escape(word) + r"\s*" + _NUM)
        for m in pattern.finditer(text):
            found.append((m.start(), operator, _scale_number(m.group(1), m.group(2))))
    for m in _SUFFIX_RE.finditer(text):
        operator = ">=" if m.group(3) == "以上" else "<="
        found.append((m.start(), operator, _scale_number(m.group(1), m.group(2))))

    results: List[Dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for pos, operator, value in found:
        metric = _nearest_metric(pos, positions)
        if metric is None or operator not in _ALLOWED_OPS:
            continue
        key = (metric, operator)
        if key in seen:
            continue
        seen.add(key)
        results.append({"metric": metric, "operator": operator, "value": value})
    return results
