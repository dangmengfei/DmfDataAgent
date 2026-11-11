"""归因节点：主指标概况 + 维度值归因 + 规则总结。

迁移自老 plugin/diagnosis/ 的 Diagnosis 编排核心
（get_metric_info_output / get_dim_cause_data / get_contribution_list /
get_smart_summary_rule）。复用 execute_query 已取回的明细数据做纯 Python 归因
（核心计算见 dmf_agent/diagnosis/core.py），输出写入 state.diagnosis
供 synthesize_answer 引用。

TODO(v2-migration) 遗留（见 MIGRATION Phase 4c）：
- 因子归因（DiagnosisMetric，需指标血缘公式元数据）
- 指标树相关性（DiagnosisTree，需 metrics_tree 表）
- 行动建议（get_action_advice，需知识库）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState
from dmf_agent.diagnosis.core import (
    contribution_top,
    dim_attribution,
    global_summary,
    metric_change,
    rows_to_dicts,
    smart_summary,
)

logger = logging.getLogger(__name__)


def _diagnose(query_result: Dict[str, Any], dsl: Dict[str, Any]) -> Dict[str, Any]:
    """纯归因计算（可单测）：返回 state.diagnosis 结构。"""
    columns = query_result.get("columns") or []
    rows = rows_to_dicts(query_result)
    if not rows or not columns:
        return {"ok": False, "message": "无可用取数数据，无法进行归因"}

    time_col = columns[0]  # 首列为时间（统计时间 / date）
    metrics = [m.get("name") for m in (dsl.get("metrics") or []) if m.get("name")]
    dims = [d for d in (dsl.get("dimensions") or []) if d in columns]
    metric = metrics[0] if metrics else None
    if metric is None or metric not in columns:
        return {"ok": False, "message": "取数结果中缺少指标列，无法进行归因"}

    change = metric_change(rows, time_col, metric)
    if change is None:
        return {
            "ok": False,
            "message": f"指标【{metric}】在取数范围内不足两个有数据的时间点，无法进行归因",
        }

    dim_results = {
        dim: dim_attribution(rows, time_col, dim, metric) for dim in dims
    }
    # 首个维度的归因结果用于同向/反向贡献 TOP 与规则总结
    primary_dim = next((d for d in dims if dim_results[d]), None)
    primary_results = dim_results.get(primary_dim, []) if primary_dim else []
    contrib = contribution_top(primary_results, change["change_value"])

    return {
        "ok": True,
        "metric": metric,
        "global_summary": global_summary(change),
        "change": change,
        "dimensions": dim_results,
        "same_contribution_list": contrib["same"],
        "opposite_contribution_list": contrib["opposite"],
        "smart_summary": smart_summary(
            change, contrib["same"], contrib["opposite"], global_summary(change)
        ),
        "primary_dim": primary_dim,
        # FR3：基于确定性贡献 TOP 生成的行动建议线索（无知识库依赖，供 LLM 组织建议）
        "action_hints": _build_action_hints(
            change, contrib["same"], contrib["opposite"], primary_dim
        ),
    }


def _build_action_hints(
    change: Dict[str, Any],
    same: List[Dict[str, Any]],
    opposite: List[Dict[str, Any]],
    primary_dim: Optional[str],
) -> List[str]:
    """基于确定性贡献 TOP 生成行动建议线索（FR3，无知识库依赖）。

    仅从已计算的归因结果组织可执行的排查/复用建议，不臆造数据外事实。
    """
    hints: List[str] = []
    if opposite:
        top = opposite[0]
        dim_desc = f"{top.get('dim_name')}={top.get('dim_value')}" if primary_dim else str(top)
        hints.append(
            f"优先排查反向贡献 TOP：{dim_desc}（影响值 {top.get('change_value')}），"
            f"定位其业务动作是否异常或存在外部冲击。"
        )
    if same:
        top = same[0]
        dim_desc = f"{top.get('dim_name')}={top.get('dim_value')}" if primary_dim else str(top)
        hints.append(
            f"复用同向贡献 TOP 经验：{dim_desc}（正向影响 {top.get('change_value')}），"
            f"评估能否复制到其它维度值以放大整体收益。"
        )
    if opposite and same:
        hints.append(
            "整体指标由同向维度值拉动、被反向维度值拖累，建议资源向正向单元倾斜、"
            "对反向单元做专项诊断。"
        )
    return hints


def build_diagnose_node(deps: AgentDeps):
    async def diagnose(state: AgentState) -> Dict[str, Any]:
        query_result = state.get("query_result") or {}
        dsl = state.get("dsl") or {}
        result = _diagnose(query_result, dsl)
        logger.info("归因结果：%s", result)
        return {"diagnosis": result}

    return diagnose
