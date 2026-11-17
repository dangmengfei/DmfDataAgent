"""归因计算核心（纯 Python，迁移自老 plugin/diagnosis/）。

v2 直接复用 execute_query 已取回的明细（columns/rows 含 统计时间 + 维度 + 指标），
无需像老系统那样为归因单独发 `dimFactorAnalysisQry` 取数：
- metric_change：主指标概况（起止值 / 变化量 / 变化率，老 get_metric_info_output）
- dim_attribution：维度值归因（按维值分组比较起止时间点，老 DiagnosisDim.single_dim_cause）
- contribution_top：同向/反向贡献 TOP（老 get_contribution_list）
- global_summary / smart_summary：规则总结（老 get_metric_info_output / get_smart_summary_rule）

2026-11-12 归因优化 P1（期间对比 + 结构变化标注，见 doc/归因分析优化方案.md）：
- period_change：本期（时间轴后半段）vs 上期（前半段）的期间对比，替代端点对比作为默认口径
  （端点对比把「近 30 天」拿第 1 天与第 30 天比，单日噪声直接决定涨跌结论）；
- period_dim_attribution：期间口径的维度值归因（本期总量 vs 上期总量），并标注
  维度成员新增（added，上期为 0）/ 消失（removed，本期为 0）/ 持续（kept）；
- 端点对比实现保留（mode="endpoint" 降级开关与单时间点兜底仍走旧逻辑）。

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


def _ordered_values(*aggs: Dict[Any, float]) -> List[Any]:
    """多个聚合字典的键并集，**顺序确定**：按各字典的插入顺序依次追加，
    首次出现的位置即最终位置（相同值只保留第一次）。

    set 的迭代顺序随哈希种子变化，会让并列 |变化量| 的 TOP 维值在不同运行间漂移；
    归因结论必须可复现，故显式保序。
    """
    out: List[Any] = []
    seen: set = set()
    for agg in aggs:
        for key in agg:
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


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


# ---------------- 期间对比（2026-11-12 归因优化 P1） ----------------


def _period_total(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str, times: List[Any]
) -> float:
    """一组时间点上的指标总量（本期/上期聚合口径：期间内所有时间点求和）。"""
    window = set(times)
    total = 0.0
    for r in rows:
        if r.get(time_col) in window and isinstance(r.get(metric_col), (int, float)):
            total += r[metric_col]
    return total


def _group_sum_times(
    rows: List[Dict[str, Any]],
    time_col: str,
    dim_col: str,
    metric_col: str,
    times: set,
) -> Dict[Any, float]:
    """一组时间点（期间）内按维度值分组求和（period_dim_attribution 用）。"""
    agg: Dict[Any, float] = {}
    for r in rows:
        if r.get(time_col) not in times:
            continue
        value = r.get(dim_col)
        v = r.get(metric_col)
        if value is None or not isinstance(v, (int, float)):
            continue
        agg[value] = agg.get(value, 0.0) + v
    return agg


def period_change(
    rows: List[Dict[str, Any]], time_col: str, metric_col: str
) -> Optional[Dict[str, Any]]:
    """期间对比的主指标概况（P1 默认口径）。

    时间轴按有序去重时间点**对半分**：后半段 = 本期，前半段 = 上期。
    对比口径从「首尾两个时间点」升级为「两段期间总量」，单日噪声（周末/大促/补数）
    不再直接决定涨跌结论。

    返回结构与 metric_change 保持同形（metrics_value=本期、compare_metrics_value=上期、
    change_value=本期-上期、change_rate 同向），另附 period 元信息（起点/终点/时间点数），
    供答复节点区分口径表述。时间点不足 2 个时返回 None（回落端点逻辑）。
    """
    times = _time_sorted_values(rows, time_col, metric_col)
    if len(times) < 2:
        return None
    mid = len(times) // 2
    prev_times, curr_times = times[:mid], times[mid:]
    curr_total = _period_total(rows, time_col, metric_col, curr_times)
    prev_total = _period_total(rows, time_col, metric_col, prev_times)
    change = curr_total - prev_total
    return {
        "metric_name": metric_col,
        "begin_time": curr_times[0],
        "end_time": curr_times[-1],
        "metrics_value": curr_total,
        "compare_metrics_value": prev_total,
        "change_value": change,
        "change_rate": round(change / prev_total, 4) if prev_total else None,
        # 期间对比元信息：本期/上期窗口与时间点数（答复口径表述用）
        "compare_mode": "period",
        "period": {
            "curr_start": curr_times[0],
            "curr_end": curr_times[-1],
            "prev_start": prev_times[0],
            "prev_end": prev_times[-1],
            "curr_points": len(curr_times),
            "prev_points": len(prev_times),
        },
    }


def period_dim_attribution(
    rows: List[Dict[str, Any]], time_col: str, dim_col: str, metric_col: str
) -> List[Dict[str, Any]]:
    """期间口径的维度值归因（P1）。

    与 dim_attribution 的差别：按**期间总量**对比（本期时间点集合内分组求和 vs
    上期时间点集合内分组求和），而非仅比较起止两个时间点；并按成员 presence 分类：
    - added：上期无数据、本期有（新开门店/新增品类等）——不与涨跌混排，单独列示；
    - removed：本期无数据、上期有（下架/关店等）——同上；
    - kept：两期都有——正常涨跌归因主体。

    排序：kept 成员按 |变化量| 降序在前，added/removed 附加列表在各自段内同样降序。
    """
    times = _time_sorted_values(rows, time_col, metric_col)
    if len(times) < 2:
        return []
    mid = len(times) // 2
    prev_times, curr_times = set(times[:mid]), set(times[mid:])
    prev_agg = _group_sum_times(rows, time_col, dim_col, metric_col, prev_times)
    curr_agg = _group_sum_times(rows, time_col, dim_col, metric_col, curr_times)
    if not prev_agg and not curr_agg:
        return []

    total_curr = sum(curr_agg.values())
    total_prev = sum(prev_agg.values())
    diff_total = total_curr - total_prev

    kept: List[Dict[str, Any]] = []
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    # 维值顺序必须确定：set 迭代顺序随哈希种子漂移，会让并列 |变化量| 的 TOP 维值
    # 每次运行都可能不同（违反「结果确定」约定）。故按「上期/本期出现顺序 + 字符串
    # 兜底」构造有序键列表再排序。
    for value in _ordered_values(prev_agg, curr_agg):
        b = prev_agg.get(value, 0.0)
        e = curr_agg.get(value, 0.0)
        change = e - b
        if value not in prev_agg or b == 0:
            presence = "added"
        elif value not in curr_agg or e == 0:
            presence = "removed"
        else:
            presence = "kept"
        item = {
            "dim_name": dim_col,
            "dim_value": value,
            "metrics_value": round(b, 4),
            "compare_metrics_value": round(e, 4),
            "change_value": round(change, 4),
            "change_rate": round(change / b, 4) if b else None,
            "effect_index": (
                round(change / abs(diff_total), 4) if diff_total != 0 else 0.0
            ),
            "presence": presence,
        }
        if math.isinf(item["effect_index"]) or math.isnan(item["effect_index"]):
            item["effect_index"] = 0.0
        if presence == "added":
            added.append(item)
        elif presence == "removed":
            removed.append(item)
        else:
            kept.append(item)

    for group in (kept, added, removed):
        group.sort(key=lambda x: abs(x["change_value"]), reverse=True)

    # 影响占比仅在 kept 段内归一（added/removed 已单独列示，混排会稀释可读性）
    total_effect = sum(abs(r["effect_index"]) for r in kept)
    for r in kept:
        r["effect_rate"] = (
            round(abs(r["effect_index"]) / total_effect, 4) if total_effect else 0.0
        )
    return kept + added + removed


def cross_attribution(
    rows: List[Dict[str, Any]],
    time_col: str,
    dim_a: str,
    dim_b: str,
    metric_col: str,
    max_combos: int = 500,
) -> Optional[Dict[str, Any]]:
    """两维交叉归因（2026-11 P2，方案 §6.1）：按 (dim_a值, dim_b值) 组合做期间对比。

    单维归因回答「渠道=APP 跌了」，交叉归因回答「APP 里哪个城市跌的」——
    对每个 dim_a 值输出期间总变化 + 其内部按 dim_b 拆分的贡献列表（children）。

    - cells 按 |总变化| 降序、children 按 |变化量| 降序；
    - children 含 presence（kept/added/removed），与单维归因同口径；
    - 组合基数（distinct (a,b) 对）超过 max_combos 时按 cell 顺序（|总变化| 降序）
      保留**整 cell** 并标注 truncated=True（防大基数维度组合爆炸；不切半 cell，
      保持「每个 a 值的 children 可加和还原该值总变化」的不变量）；
    - 时间点不足 2 个 / 无组合数据返回 None（调用方跳过交叉段）。
    """
    times = _time_sorted_values(rows, time_col, metric_col)
    if len(times) < 2:
        return None
    mid = len(times) // 2
    prev_times, curr_times = set(times[:mid]), set(times[mid:])

    # (a, b) 组合期间聚合
    prev_agg: Dict[Any, float] = {}
    curr_agg: Dict[Any, float] = {}
    for r in rows:
        t = r.get(time_col)
        if t is None:
            continue
        a, b, v = r.get(dim_a), r.get(dim_b), r.get(metric_col)
        if a is None or b is None or not isinstance(v, (int, float)):
            continue
        key = (a, b)
        if t in prev_times:
            prev_agg[key] = prev_agg.get(key, 0.0) + v
        elif t in curr_times:
            curr_agg[key] = curr_agg.get(key, 0.0) + v
    if not prev_agg and not curr_agg:
        return None

    cells: Dict[Any, Dict[str, Any]] = {}
    for key in _ordered_values(prev_agg, curr_agg):
        a, b = key
        p = prev_agg.get(key, 0.0)
        c = curr_agg.get(key, 0.0)
        if key not in prev_agg or p == 0:
            presence = "added"
        elif key not in curr_agg or c == 0:
            presence = "removed"
        else:
            presence = "kept"
        change = c - p
        cell = cells.setdefault(
            a, {"dim_a_value": a, "prev": 0.0, "curr": 0.0, "children": []}
        )
        cell["prev"] += p
        cell["curr"] += c
        cell["children"].append(
            {
                "dim_name": dim_b,
                "dim_value": b,
                "metrics_value": round(p, 4),
                "compare_metrics_value": round(c, 4),
                "change_value": round(change, 4),
                "change_rate": round(change / p, 4) if p else None,
                "presence": presence,
            }
        )

    cell_list = list(cells.values())
    for cell in cell_list:
        cell["prev"] = round(cell["prev"], 4)
        cell["curr"] = round(cell["curr"], 4)
        cell["change_value"] = round(cell["curr"] - cell["prev"], 4)
        cell["change_rate"] = (
            round(cell["change_value"] / cell["prev"], 4) if cell["prev"] else None
        )
        cell["children"].sort(key=lambda x: abs(x["change_value"]), reverse=True)
    cell_list.sort(key=lambda x: abs(x["change_value"]), reverse=True)

    total_combos = sum(len(c["children"]) for c in cell_list)
    truncated = False
    if total_combos > max_combos:
        truncated = True
        kept_cells: List[Dict[str, Any]] = []
        acc = 0
        for cell in cell_list:
            n = len(cell["children"])
            if kept_cells and acc + n > max_combos:
                break
            kept_cells.append(cell)
            acc += n
        cell_list = kept_cells

    return {
        "dim_a": dim_a,
        "dim_b": dim_b,
        "cells": cell_list,
        "total_combos": total_combos,
        "truncated": truncated,
    }


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
    for value in _ordered_values(begin_agg, end_agg):
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


def period_global_summary(change: Dict[str, Any]) -> str:
    """期间口径的整体概况（P1）：表述为「本期 vs 上期」总量对比。

    上期为 0（成员新增结构）时变化率无定义，如实说明不编造比率。
    """
    name = change["metric_name"]
    curr, prev = change["metrics_value"], change["compare_metrics_value"]
    diff = change["change_value"]
    if diff == 0:
        return f"{name}本期与上期总量持平。"
    direction = "上升" if diff > 0 else "下降"
    rate = change.get("change_rate")
    rate_text = (
        f"变化率 {format_percentage(rate)}，"
        if rate is not None
        else "上期无数据，变化率不可计算，"
    )
    return (
        f"{name}本期总量 {format_number(curr)}，上期总量 {format_number(prev)}，"
        f"{direction} {format_number(abs(diff))}，{rate_text}"
        f"实际业务表现变{'好' if diff > 0 else '差'}了。"
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


def period_smart_summary(
    change: Dict[str, Any],
    same: List[Dict[str, Any]],
    opposite: List[Dict[str, Any]],
    global_text: str,
    added: Optional[List[Dict[str, Any]]] = None,
    removed: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """期间口径的规则总结（P1）。

    与 smart_summary 的差别：口径表述为「本期（a~b）vs 上期（c~d）」；
    added/removed（成员结构变化）单独成句，不与涨跌归因混排。
    """
    period = change.get("period") or {}
    curr = (
        f"本期（{period.get('curr_start', '?')}~{period.get('curr_end', '?')}，"
        f"{period.get('curr_points', '?')} 个时间点）"
    )
    prev = (
        f"上期（{period.get('prev_start', '?')}~{period.get('prev_end', '?')}，"
        f"{period.get('prev_points', '?')} 个时间点）"
    )
    head = f"对比口径：{curr} vs {prev}。{global_text}"

    change_value = change.get("change_value", 0)
    if change_value == 0:
        return head + "两期总量持平。"

    direction = "上升" if change_value > 0 else "下降"
    same_dims = "、".join(f"{i.get('dim_name')}是{i.get('dim_value')}" for i in same)
    opposite_dims = "、".join(f"{i.get('dim_name')}是{i.get('dim_value')}" for i in opposite)
    text = head + f"主指标出现【{direction}】"
    if same_dims:
        text += f"，主要{'得益于' if change_value > 0 else '受到'}{same_dims}的{'上升' if change_value > 0 else '下滑'}"
    text += "。"
    if opposite_dims:
        text += (
            f"同时{opposite_dims}出现{'下降' if change_value > 0 else '上升'}，"
            f"{'部分抵消了' if change_value > 0 else '未能阻止'}整体变化。"
        )
    add_names = "、".join(str(i.get("dim_value")) for i in (added or [])[:3])
    rem_names = "、".join(str(i.get("dim_value")) for i in (removed or [])[:3])
    if add_names:
        text += f"此外，{add_names}为本期新增成员（上期无数据）。"
    if rem_names:
        text += f"{rem_names}本期无数据（成员消失），已单独列示。"
    return text
