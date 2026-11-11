"""归因计算核心（纯 Python，迁移自老 plugin/diagnosis/）。

v2 直接复用 execute_query 已取回的明细（columns/rows 含 统计时间 + 维度 + 指标），
无需像老系统那样为归因单独发 `dimFactorAnalysisQry` 取数：
- metric_change：主指标概况（起止值 / 变化量 / 变化率，老 get_metric_info_output）
- dim_attribution：维度值归因（按维值分组比较起止时间点，老 DiagnosisDim.single_dim_cause）
- contribution_top：同向/反向贡献 TOP（老 get_contribution_list）
- global_summary / smart_summary：规则总结（老 get_metric_info_output / get_smart_summary_rule）

未覆盖（见 MIGRATION Phase 4c 遗留）：因子归因（需指标血缘公式元数据）、
指标树相关性（需 metrics_tree 表）、行动建议（需知识库）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def rows_to_dicts(query_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """{columns, rows} -> 行字典列表（归因计算以 dict 行为单位）。"""
    columns = query_result.get("columns") or []
    rows = query_result.get("rows") or []
    return [dict(zip(columns, row)) for row in rows]


def _time_sorted_values(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str
) -> List[Any]:
    """按时间列去重取序（仅统计该指标有值的行）。"""
    seen: set = set()
    times: List[Any] = []
    for r in rows:
        t = r.get(time_col)
        if t is None or r.get(metric_col) is None or t in seen:
            continue
        seen.add(t)
        times.append(t)
    return times


def _value_at(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str, target: Any
) -> Optional[float]:
    """取某时间点的指标总值（跨维度行求和；老系统主指标用无维度取数得总量）。"""
    total = 0.0
    found = False
    for r in rows:
        if r.get(time_col) == target and isinstance(r.get(metric_col), (int, float)):
            total += r[metric_col]
            found = True
    return total if found else None


def metric_change(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str
) -> Optional[Dict[str, Any]]:
    """主指标概况（老 get_metric_info_output）：起止值/变化量/变化率。

    单时间点（不足两个有值时间）无法归因，返回 None。
    """
    times = _time_sorted_values(rows, time_col, metric_col)
    if len(times) < 2:
        return None
    begin_t, end_t = times[0], times[-1]
    begin_val = _value_at(rows, time_col, metric_col, begin_t)
    end_val = _value_at(rows, time_col, metric_col, end_t)
    if begin_val is None or end_val is None:
        return None
    change = end_val - begin_val
    return {
        "metric_name": metric_col,
        "begin_time": begin_t,
        "end_time": end_t,
        "metrics_value": begin_val,
        "compare_metrics_value": end_val,
        "change_value": change,
        "change_rate": round(change / begin_val, 4) if begin_val else 0,
    }


def _group_sum(
    rows: List[Dict[str, Any]],
    time_col: str,
    dim_col: str,
    metric_col: str,
    target: Any,
) -> Dict[Any, float]:
    """某时间点按维度值分组求和。"""
    agg: Dict[Any, float] = {}
    for r in rows:
        if r.get(time_col) != target:
            continue
        value = r.get(dim_col)
        v = r.get(metric_col)
        if value is None or not isinstance(v, (int, float)):
            continue
        agg[value] = agg.get(value, 0.0) + v
    return agg


def dim_attribution(
    rows: List[Dict[str, Any]], time_col: str, dim_col: str, metric_col: str
) -> List[Dict[str, Any]]:
    """维度值归因（老 DiagnosisDim.single_dim_cause）。

    每个维度值按起止时间点分组求和，计算变化量/变化率/影响指数/影响占比，
    按 |变化量| 降序返回。
    """
    times = _time_sorted_values(rows, time_col, metric_col)
    if len(times) < 2:
        return []
    begin_t, end_t = times[0], times[-1]
    begin_agg = _group_sum(rows, time_col, dim_col, metric_col, begin_t)
    end_agg = _group_sum(rows, time_col, dim_col, metric_col, end_t)
    if not begin_agg or not end_agg:
        return []

    total_begin = sum(begin_agg.values())
    total_end = sum(end_agg.values())
    diff_total = total_end - total_begin

    results: List[Dict[str, Any]] = []
    for value in set(begin_agg) | set(end_agg):
        b = begin_agg.get(value, 0.0)
        e = end_agg.get(value, 0.0)
        change = e - b
        effect_index = change / abs(diff_total) if diff_total != 0 else 0.0
        if math.isinf(effect_index) or math.isnan(effect_index):
            effect_index = 0.0
        results.append(
            {
                "dim_name": dim_col,
                "dim_value": value,
                "metrics_value": round(b, 4),
                "compare_metrics_value": round(e, 4),
                "change_value": round(change, 4),
                "change_rate": round(change / b, 4) if b else 0.0,
                "effect_index": round(effect_index, 4),
            }
        )

    total_effect = sum(abs(r["effect_index"]) for r in results)
    for r in results:
        r["effect_rate"] = round(abs(r["effect_index"]) / total_effect, 4) if total_effect else 0.0
    results.sort(key=lambda x: abs(x["change_value"]), reverse=True)
    return results


def contribution_top(
    dim_results: List[Dict[str, Any]],
    metric_change_value: Optional[float] = None,
    top_n: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """同向/反向贡献 TOP（老 get_contribution_list）。

    指标整体上升时 change_value>=0 为同向；整体下降时反向（正负对调）。
    """
    same = [r for r in dim_results if r["change_value"] >= 0]
    opposite = [r for r in dim_results if r["change_value"] < 0]
    if metric_change_value is not None and metric_change_value < 0:
        same, opposite = opposite, same
    return {"same": same[:top_n], "opposite": opposite[:top_n]}


def format_number(value: float) -> str:
    """数值格式化：超过 1 万转 xxx 万（老 utils.format_number）。"""
    return f"{round(value / 10000, 3):,}万" if abs(value) >= 10000 else f"{value:,}"


def format_percentage(value: float) -> str:
    return f"{round(value * 100, 2)}%"


def global_summary(change: Dict[str, Any]) -> str:
    """归因整体概况（老 get_metric_info_output 的 global_summary 文本）。"""
    name = change["metric_name"]
    begin, end = change["metrics_value"], change["compare_metrics_value"]
    diff = change["change_value"]
    if diff == 0:
        return f"{name}在这期间数据没有发生变化。"
    direction = "上升" if end > begin else "下降"
    return (
        f"{name}在这期间从{format_number(begin)}变为{format_number(end)}，"
        f"{direction}了{format_number(abs(diff))}，"
        f"变化率为{format_percentage(change['change_rate'])}。"
        f"实际业务表现变{'好' if end > begin else '差'}了。"
    )


def smart_summary(
    change: Dict[str, Any],
    same: List[Dict[str, Any]],
    opposite: List[Dict[str, Any]],
    global_text: str,
) -> str:
    """规则智能总结（老 get_smart_summary_rule）。"""
    same_dims = "、".join(f"{i.get('dim_name')}是{i.get('dim_value')}" for i in same)
    opposite_dims = "、".join(f"{i.get('dim_name')}是{i.get('dim_value')}" for i in opposite)
    begin_time, end_time = change["begin_time"], change["end_time"]
    change_value = change["change_value"]

    if change_value == 0:
        return f"从{begin_time}到{end_time}，{global_text}"

    if change_value > 0:
        text = (
            f"从{begin_time}到{end_time}，主指标出现【上升】，这主要得益于{same_dims}的上升。"
            if same_dims
            else f"从{begin_time}到{end_time}，主指标出现【上升】。"
        )
        if opposite_dims:
            text += f"但同时也要注意到，{opposite_dims}出现下降的情况。"
        return text

    text = (
        f"从{begin_time}到{end_time}，主指标出现【下降】，主要受到{same_dims}的影响。"
        if same_dims
        else f"从{begin_time}到{end_time}，主指标出现【下降】。"
    )
    if opposite_dims:
        text += f"尽管{opposite_dims}有所上升，但是未能阻止整体指标的下滑。"
    return text
