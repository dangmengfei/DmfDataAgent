"""要素校验节点：指标/维度是否收录、维度是否被指标数据域支持、粒度与同环比是否在范围内。

校验源为**语义层**（manifest.yml），不再依赖指标元数据仓储 / 指标平台：
- 指标未收录 -> 追问（走 ask_clarification 分支）
- 维度不被指标所在数据域支持 -> 追问（例如"日活跃用户数"没有"地区"维度）
- 时间粒度 / 同环比类型超出指标白名单 -> 追问
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.guidance import NO_METRIC_PROBLEM as _NO_METRIC_PROBLEM
from dmf_agent.core.state import AgentState

logger = logging.getLogger(__name__)


def _collect_missed_metrics(deps: AgentDeps, elements: Dict[str, Any]) -> list[str]:
    """挑出「语义层未收录」的指标名（FR8：未命中 -> 引导用户创建，不自动创建）。

    两个来源，合并去重、保序：
    1. `unrecognized_metrics`（LLM 按 prompt 要求单独报告"用户提到但候选集没有"的指标名，
       主渠道——因为 `metrics` 被约束为只能来自候选集，不会带出未收录名）；
    2. `metrics` 里 resolve 不到的名字（LLM 未遵守约束时的兜底，不放过）。

    纯「没识别出指标」不算未命中（那属于要素缺失），交由通用引导处理。
    """
    layer = deps.semantic_layer
    if layer is None:
        # 语义层未装配时无从判定收录情况，不引导（避免误报未收录）
        return []
    names = list(elements.get("unrecognized_metrics") or []) + list(
        elements.get("metrics") or []
    )
    missed: list[str] = []
    for name in names:
        text = str(name).strip()
        if not text or text in missed:
            continue
        try:
            if layer.resolve_metric(text) is None:
                missed.append(text)
        except Exception as exc:  # noqa: BLE001 - 解析失败不应阻断校验
            logger.warning("指标[%s]解析失败，跳过未命中统计：%s", text, exc)
    return missed


def build_validate_elements_node(deps: AgentDeps):
    async def validate_elements(state: AgentState) -> Dict[str, Any]:
        elements = state.get("query_elements") or {}
        problems: list[str] = []

        if deps.semantic_layer is None:
            logger.warning("语义层未装配，跳过要素校验")
        else:
            problems = deps.semantic_layer.validate_elements(elements)

        if not problems and not (elements.get("metrics") or []):
            problems.append("未能从问题中识别出任何业务指标")

        # FR8：语义层未收录的指标名（供 ask_clarification / synthesize_answer 引导创建）
        missed_metrics = _collect_missed_metrics(deps, elements)
        if missed_metrics:
            # 文案修正：用户确实提到了指标（只是未收录），不该说"没识别出任何指标"
            problems = [p for p in problems if p != _NO_METRIC_PROBLEM]
            for name in missed_metrics:
                text = f"指标[{name}]未收录在语义层"
                if text not in problems:
                    problems.append(text)

        # 低置信澄清：已识别到指标（无其它硬伤）但抽取置信度偏低时，请用户确认，避免误取数。
        confidence = elements.get("confidence")
        threshold = deps.agent_settings.low_confidence_threshold
        if (
            not problems
            and isinstance(confidence, (int, float))
            and float(confidence) < threshold
        ):
            problems.append(
                f"问题中指标/口径的识别置信度较低（{float(confidence):.2f}），请确认要查询的具体指标与口径"
            )

        ok = not problems
        logger.info(
            "要素校验：%s（problems=%s，missed_metrics=%s）",
            "通过" if ok else "不通过",
            problems,
            missed_metrics,
        )
        return {
            "validation": {
                "ok": ok,
                "problems": problems,
                "missed_metrics": missed_metrics,
            }
        }

    return validate_elements
