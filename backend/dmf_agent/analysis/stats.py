"""结果集统计分析（从原 tools/hm_api.py 迁出，与取数实现解耦）。

只依赖 `{columns, rows}` 结构，不感知数据来源（StarRocks / 其它后端）。
"""

from __future__ import annotations

import re
from typing import Any

# 时间列名（统计时跳过；语义层的时间列别名与常见来源列名）
_TIME_COLUMN_NAMES = {"date", "日期", "统计日期", "统计时间", "时间", "dt", "month", "week"}
_DATE_LIKE_RE = re.compile(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?")


def _is_time_column(name: str, values) -> bool:
    if str(name) in _TIME_COLUMN_NAMES:
        return True
    sample = next((v for v in values if v is not None), None)
    return isinstance(sample, str) and bool(_DATE_LIKE_RE.match(sample))


def compute_stats(query_result: dict[str, Any]) -> dict[str, dict[str, float]]:
    """对查询结果按指标列计算基础统计量（时间列与非数值列自动跳过）。"""
    columns: list[str] = query_result.get("columns", [])
    rows: list[list[Any]] = query_result.get("rows", [])
    stats: dict[str, dict[str, float]] = {}
    for idx, col in enumerate(columns):
        values = [r[idx] for r in rows if idx < len(r) and isinstance(r[idx], (int, float))]
        if not values or _is_time_column(col, values):
            continue
        stats[col] = {
            "total": round(sum(values), 2),
            "avg": round(sum(values) / len(values), 2),
            "max": round(max(values), 2),
            "min": round(min(values), 2),
        }
    return stats


def describe_data(query_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """描述性统计 + 变异系数（迁移自老 df_analysis_report 的 df.describe() + cv_info）。

    报告生成用：count/mean/std/min/max + cv（|std/mean|，衡量数据波动；
    老系统取 cv 最大的 top5 作为"值得关注的指标"）。
    """
    columns: list[str] = query_result.get("columns", [])
    rows: list[list[Any]] = query_result.get("rows", [])
    result: dict[str, dict[str, Any]] = {}
    for idx, col in enumerate(columns):
        values = [r[idx] for r in rows if idx < len(r) and isinstance(r[idx], (int, float))]
        if not values or _is_time_column(col, values):
            continue
        mean = sum(values) / len(values)
        std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        result[col] = {
            "count": len(values),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "cv": round(abs(std / mean), 4) if mean else 0,
        }
    return result
