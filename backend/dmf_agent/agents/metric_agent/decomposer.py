"""问题分解（FR10 的 Orchestrator 前置）：把复杂问题拆成多个子任务。

默认走**规则分解**（确定性、可测试）：当问题含拆分标记（、/和/以及/分别/对比…）
且能从语义层识别出 ≥2 个指标时，按每个识别出的指标生成一个子任务；
否则返回单个子任务（不拆分，主图走常规单链路）。

LLM 可用时（get_chat_model 非 None）可尝试结构化分解，失败回退规则。
"""

from __future__ import annotations

import logging
import re
from typing import Any, List

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.subagent_protocol import SubTask

logger = logging.getLogger(__name__)

# 拆分标记：并列 / 对比类连词或关键词
_SPLIT_MARKERS = re.compile(r"[、，,与和及以及]+|分别|对比|对照|各自|同时")
# 子问题模板（聚焦单个指标，避免引入父问题无关上下文）
_SUBQ_TEMPLATE = "{metric}在相关维度下的表现如何？"


def _detect_metrics(text: str, deps: AgentDeps) -> List[str]:
    """从问句中识别已收录的指标（含别名匹配），按出现顺序去重。"""
    layer = deps.semantic_layer
    if layer is None:
        return []
    found: List[str] = []
    for m in layer.list_metrics():
        name = m.name
        if name and name in text and name not in found:
            found.append(name)
            continue
        for alias in getattr(m, "aliases", []) or []:
            if alias and alias in text and m.name not in found:
                found.append(m.name)
                break
    return found


def decompose_query(
    question: Any,
    *,
    deps: AgentDeps,
    max_subtasks: int = 6,
    allow_llm: bool = True,
) -> List[SubTask]:
    """把一个问题分解为子任务列表。

    - 含拆分标记且识别到 ≥2 个指标 -> 每个指标一个子任务（深度 0）
    - 否则 -> 单个子任务（不拆分，主图常规路径处理）
    """
    text = question if isinstance(question, str) else str(question)
    metrics = _detect_metrics(text, deps)
    has_marker = bool(_SPLIT_MARKERS.search(text))

    if has_marker and len(metrics) >= 2:
        tasks: List[SubTask] = []
        for i, m in enumerate(metrics[:max_subtasks]):
            tasks.append(
                SubTask(
                    id=f"st-{i + 1}",
                    question=_SUBQ_TEMPLATE.format(metric=m),
                    intent="metric_query",
                    depth=0,
                    hints={"metrics": [m]},
                )
            )
        logger.info("问题分解为 %d 个子任务：%s", len(tasks), [t.question for t in tasks])
        return tasks

    # 单任务：不拆分
    return [SubTask(id="st-1", question=text, intent="metric_query", depth=0)]
