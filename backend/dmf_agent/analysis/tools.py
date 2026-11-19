"""分析模型统一执行器（run_analysis 工具实现，2026-11-18 P1，方案 §3.2）。

- `run_analysis(query_result, model, params)`：查 MODEL_REGISTRY → 参数基础校验 →
  调 analyze() → 异常捕获转 `AnalysisResult(ok=false)`；
- `run_analysis_tool(args)`：Harness 工具 `run_analysis` 的执行体（白名单/权限/审计/
  超时由 harness.invoke 外壳保证；query_result 由调用方经 args 传入）；
- `keyword_plan(text)`：关键词规则表选模型（LLM 规划的兜底，P2 接入 analysis_plan）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from dmf_agent.analysis.base import (
    AnalysisResult,
    get_model,
    model_names,
)
from dmf_agent.harness.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 关键词规则表（顺序即优先级；「分布」沿用 FR2 既有语义）
_KEYWORD_RULES: List[Tuple[str, str]] = [
    ("趋势", "analysis_trend"),
    ("走势", "analysis_trend"),
    ("异常", "analysis_anomaly"),
    ("突增", "analysis_anomaly"),
    ("突降", "analysis_anomaly"),
    ("相关", "analysis_correlation"),
    ("二八", "analysis_pareto"),
    ("头部", "analysis_pareto"),
    ("集中", "analysis_pareto"),
    ("对比", "analysis_compare"),
    ("相比", "analysis_compare"),
    ("分布", "analysis_distribution"),
]


def run_analysis(
    query_result: Dict[str, Any],
    model: str,
    params: Optional[Dict[str, Any]] = None,
    registry: Optional[ToolRegistry] = None,
) -> AnalysisResult:
    """执行指定分析模型（同步；模型实现均为纯计算）。

    - 模型未注册 / 参数不合法：返回 ok=false 的结构化结果（不抛错）；
    - 模型执行异常：捕获转 ok=false（绝不中断主链路）；
    - registry 提供时用其 validate_arguments 做参数校验（与 harness 同一套逻辑）。
    """
    name = str(model or "").strip()
    m = get_model(name)
    if m is None:
        return AnalysisResult(
            ok=False,
            model=name,
            summary="",
            error=f"未注册的分析模型：{name}（可用：{'/'.join(model_names())}）",
        )
    p = dict(params or {})
    if registry is not None:
        validation = registry.validate_arguments(name, p)
        if not validation.get("ok"):
            return AnalysisResult(
                ok=False, model=name, summary="", error=f"参数校验失败：{validation.get('error')}"
            )
    try:
        return m.analyze(query_result, p)
    except Exception as exc:  # noqa: BLE001 - 模型异常统一转结构化失败
        logger.warning("分析模型[%s]执行失败：%s", name, exc, exc_info=True)
        return AnalysisResult(ok=False, model=name, summary="", error=f"模型执行失败：{exc}")


def run_analysis_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    """Harness 工具 `run_analysis` 的执行体（harness/registry.py 注册）。

    args 契约：{model: str, params?: dict, query_result?: dict}——query_result 由
    调用方（analyze_data 节点 / Skill 步骤）显式传入，工具不感知 state。
    """
    qr = args.get("query_result")
    if not isinstance(qr, dict):
        return AnalysisResult(
            ok=False,
            model=str(args.get("model") or ""),
            summary="",
            error="run_analysis 需要上一步的 query_result（对象）",
        ).to_dict()
    result = run_analysis(qr, str(args.get("model") or ""), args.get("params"))
    return result.to_dict()


def keyword_plan(text: str) -> List[str]:
    """关键词规则选模型（LLM 规划失败/无 LLM 时的兜底；命中即单步计划）。

    返回模型名列表（当前最多 1 步）；未命中返回空列表（调用方保持现状：stats 兜底）。
    """
    text = str(text or "")
    for keyword, model in _KEYWORD_RULES:
        if keyword in text:
            return [model]
    return []


def plan_models_text() -> str:
    """可用模型清单文本（喂给 LLM 规划 / 管理面展示）。"""
    lines = []
    for name in model_names():
        m = get_model(name)
        if m is not None:
            lines.append(f"- {name}: {m.description}")
    return "\n".join(lines)
