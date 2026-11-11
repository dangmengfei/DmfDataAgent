"""评测执行器：跑用例 -> 意图/要素断言 -> 准确率；结果 JSON 落盘。

迁移自老系统 sa_utils/tuner.py + auto_test*.py：
- 单用例 run_case：执行主图，断言 intent / metrics / skills
- 整组 run_suite：准确率汇总（对齐老系统回归口径）
- save_scores_to_json：评测报告写 JSON（观测已切 agent-insight + JSON 落盘）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
from dmf_agent.config.settings import Settings, get_settings

if TYPE_CHECKING:
    # 仅类型检查期导入，避免运行时循环导入；供 run_suite_with_judge /
    # eval_report_to_dict 的 EvalReport 注解使用
    from dmf_agent.eval.metrics import EvalReport
from dmf_agent.eval.cases import DEFAULT_CASES, EvalCase
from dmf_agent.experience import default_example_store
from dmf_agent.llm.providers import get_chat_model
from dmf_agent.recall import CantDictionary, DEFAULT_CANT_DICT, DimensionRecaller, FilterRecaller, MetricRecaller
from dmf_agent.semantic import MetricFlowEngine, SemanticLayer, StarRocksClient

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    case: EvalCase
    intent_ok: bool
    metrics_ok: bool
    skills_ok: bool
    actual_intent: str = ""
    actual_metrics: List[str] = field(default_factory=list)
    actual_skills: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.intent_ok and self.metrics_ok and self.skills_ok


def build_eval_deps(settings: Optional[Settings] = None) -> AgentDeps:
    """评测用依赖（语义层 + 召回器 + 经验库），真实 LLM 配置后同样可用。"""
    s = settings or get_settings()
    layer = SemanticLayer()
    return AgentDeps(
        settings=s,
        get_chat_model=lambda: get_chat_model(s),
        semantic_layer=layer,
        metric_engine=MetricFlowEngine(layer, StarRocksClient(s.effective_starrocks)),
        recaller=MetricRecaller(s.recall),
        dim_recaller=DimensionRecaller(s.recall),
        filter_recaller=FilterRecaller(s.recall),
        cant_dict=CantDictionary.from_raw_rows(DEFAULT_CANT_DICT),
        example_store=default_example_store(),
    )


async def run_case(graph, case: EvalCase) -> EvalResult:
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content=case.query)],
            "session_id": f"eval-{case.name}",
            "user_id": "eval",
        }
    )
    actual_intent = result.get("intent") or ""
    elements = result.get("query_elements") or {}
    actual_metrics = elements.get("metrics") or []
    actual_skills = result.get("skills") or []
    return EvalResult(
        case=case,
        intent_ok=actual_intent == case.expected_intent,
        metrics_ok=set(actual_metrics) == set(case.expected_metrics),
        skills_ok=set(actual_skills) == set(case.expected_skills),
        actual_intent=actual_intent,
        actual_metrics=actual_metrics,
        actual_skills=actual_skills,
    )


async def run_suite(
    cases: Optional[List[EvalCase]] = None, deps: Optional[AgentDeps] = None
) -> Dict[str, Any]:
    """跑完整组用例，返回 {total, passed, accuracy, results}。"""
    cases = cases if cases is not None else DEFAULT_CASES
    graph = build_metric_agent_graph(deps or build_eval_deps())
    results = [await run_case(graph, c) for c in cases]
    passed = sum(1 for r in results if r.passed)
    return {
        "total": len(results),
        "passed": passed,
        "accuracy": round(passed / len(results), 4) if results else 0.0,
        "results": results,
    }


def save_scores_to_json(
    report: Dict[str, Any], path: str = "data/eval/report.json"
) -> Optional[str]:
    """把评测报告（准确率 + 逐用例打分）写为 JSON 文件；失败返回 None。

    观测已切换为 agent-insight + JSON 落盘，评测结果同样以 JSON 保存供后续分析。
    """
    try:
        payload = {
            "total": report["total"],
            "passed": report["passed"],
            "accuracy": report["accuracy"],
            "results": [
                {
                    "name": r.case.name,
                    "query": r.case.query,
                    "passed": r.passed,
                    "expected_intent": r.case.expected_intent,
                    "actual_intent": r.actual_intent,
                    "actual_metrics": r.actual_metrics,
                    "actual_skills": r.actual_skills,
                }
                for r in report["results"]
            ],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断
        logger.warning("评测报告落盘失败：%s", exc)
        return None


# ---------------------------------------------------------------------------
# P1-7：可注入 answer_provider 的评测执行（不依赖全图，便于离线/单测/门禁）
# ---------------------------------------------------------------------------

async def run_suite_with_judge(
    cases: Optional[List[EvalCase]] = None,
    answer_provider: Any = None,
    judge_fn: Any = None,
) -> "EvalReport":
    """跑用例集 -> 要素断言 + 可选 judge -> 聚合 EvalReport。

    :param cases: 用例集（默认默认数据集）
    :param answer_provider: ``callable | coroutine(EvalCase) -> dict``，
        返回 ``{answer, intent, metrics, skills, time_ok, attribution_ok}``。
        注入式设计使评测**不依赖真实 LLM / StarRocks**，离线即可跑（满足 P1-7 基线度量）。
    :param judge_fn: ``async callable(query, reference, answer) -> JudgeResult``（可选）
    """
    from dmf_agent.eval.metrics import EvalCaseResult, compute_metrics

    cases = cases if cases is not None else []
    if answer_provider is None:
        raise ValueError("run_suite_with_judge 需要注入 answer_provider")

    results: List[EvalCaseResult] = []
    for case in cases:
        provided = answer_provider(case)
        if hasattr(provided, "__await__"):
            provided = await provided
        provided = provided or {}
        judge = None
        if judge_fn is not None:
            judge = judge_fn(case.query, case.reference_answer, provided.get("answer", ""))
            if hasattr(judge, "__await__"):
                judge = await judge
        results.append(
            EvalCaseResult(
                name=case.name,
                query=case.query,
                category=case.category,
                intent_ok=provided.get("intent", "") == case.expected_intent,
                metrics_ok=set(provided.get("metrics", [])) == set(case.expected_metrics),
                skills_ok=set(provided.get("skills", [])) == set(case.expected_skills),
                time_ok=bool(provided.get("time_ok", True)),
                attribution_ok=bool(provided.get("attribution_ok", True)),
                judge=judge,
                answer=provided.get("answer", ""),
                is_attribution=(case.category == "attribution"),
            )
        )
    return compute_metrics(results)


def eval_report_to_dict(report: "EvalReport") -> Dict[str, Any]:
    """EvalReport -> 普通 dict（供 JSON 落盘 / API 返回）。"""
    from dmf_agent.eval.metrics import EvalReport

    if isinstance(report, EvalReport):
        return report.to_dict()
    return dict(report)
