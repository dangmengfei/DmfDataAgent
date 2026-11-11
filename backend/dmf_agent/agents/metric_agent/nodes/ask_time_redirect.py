"""空数据时间窗重定向确认节点（FR5 闭环）。

analyze_data 已对空结果做 6 项回溯；当首因是「查询时间晚于数据截至日」且能确定性地
算出「最新可得窗口」时，图在分析后转到此节点（见 graph.py::_route_after_analysis 的
"redirect" 分支）：产出自然语言说明 + 结构化确认载荷（clarify_time_redirect）后直接
END。用户在卡片上确认 -> 带 clarify_selection 重新入图 -> apply_clarification 采用
adjusted 窗口续跑取数；选择「暂不重查」则不产生请求。

纯确定性节点（无 LLM）：窗口计算在 empty_trace.compute_time_redirect 完成，
本节点只负责组装 final_answer 与 confirmation，保证零外部依赖、行为可测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.agents.metric_agent.clarification import (
    build_time_redirect_clarification,
)
from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState

logger = logging.getLogger(__name__)


def build_ask_time_redirect_node(deps: AgentDeps):
    async def ask_time_redirect(state: AgentState) -> Dict[str, Any]:
        diagnosis = ((state.get("analysis") or {}).get("empty_diagnosis")) or {}
        redirect = diagnosis.get("redirect")
        # 路由理论上只在 redirect 存在时进入；缺失即透传（防御性，不改变拓扑语义）
        if not redirect:
            return {}

        clarification = build_time_redirect_clarification(state, diagnosis)
        original = redirect.get("original") or {}
        adjusted = redirect.get("adjusted") or {}
        as_of = redirect.get("data_as_of")
        final_answer = (
            f"您查询的时间段（{original.get('start') or '不限'} ~ "
            f"{original.get('end') or '不限'}）暂无数据，底表数据最新仅更新至 "
            f"{as_of}。\n\n可改为查询{redirect.get('label')}，请在下方卡片确认；"
            "如不需要，请点「暂不重查」或换个条件重新提问。"
        )
        logger.info(
            "空数据时间窗重定向确认：%s ~ %s -> %s ~ %s（截至 %s）",
            original.get("start"),
            original.get("end"),
            adjusted.get("start"),
            adjusted.get("end"),
            as_of,
        )
        return {
            "final_answer": final_answer,
            "confirmation": {
                "problems": clarification["problems"],
                "original_query": clarification["original_query"],
                "clarification": clarification,
            },
        }

    return ask_time_redirect
