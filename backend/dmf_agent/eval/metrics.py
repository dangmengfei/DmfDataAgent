"""评测聚合指标（P1-7）：把逐用例结果收敛为端到端报告。

- ``EvalCaseResult``：单用例结果（要素断言 + 可选 judge）。
- ``EvalReport``：端到端指标（TCR / 六要素准确率 / TOA / 归因准确率 / 幻觉率 / 平均可信度）。
- ``compute_metrics``：纯函数，输入相同输出相同——便于单测，也便于离线/定时回归复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from dmf_agent.eval.judge import JudgeResult


@dataclass
class EvalCaseResult:
    name: str
    query: str
    category: str = ""
    # 要素级断言
    intent_ok: bool = False
    metrics_ok: bool = False
    skills_ok: bool = False
    time_ok: bool = True          # TOA（时间/排序正确性），缺省不计为失败
    attribution_ok: bool = True   # 归因正确性，缺省不计为失败
    # judge（可选）
    judge: Optional[JudgeResult] = None
    answer: str = ""
    # 是否计入归因维度（仅 category=='attribution' 的用例计 attribution_accuracy）
    is_attribution: bool = False

    @property
    def passed(self) -> bool:
        base = self.intent_ok and self.metrics_ok and self.skills_ok
        if not self.time_ok:
            base = False
        if self.is_attribution and not self.attribution_ok:
            base = False
        return bool(base)


@dataclass
class EvalReport:
    total: int = 0
    passed: int = 0
    # 端到端任务完成率（TCR）：passed / total
    tcr: float = 0.0
    # 六要素准确率：intent&metrics&skills 全中的比例
    six_element_accuracy: float = 0.0
    # 各要素单独准确率
    intent_accuracy: float = 0.0
    metrics_accuracy: float = 0.0
    skills_accuracy: float = 0.0
    # 时间/排序准确率（TOA）
    toa_accuracy: float = 0.0
    # 归因准确率（仅归因类用例）
    attribution_accuracy: float = 0.0
    # 幻觉率（judge 标记 hallucination 的比例；PRD 目标 0）
    hallucination_rate: float = 0.0
    # 平均可信度（faithfulness）
    avg_faithfulness: float = 0.0
    results: List[EvalCaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "tcr": self.tcr,
            "six_element_accuracy": self.six_element_accuracy,
            "intent_accuracy": self.intent_accuracy,
            "metrics_accuracy": self.metrics_accuracy,
            "skills_accuracy": self.skills_accuracy,
            "toa_accuracy": self.toa_accuracy,
            "attribution_accuracy": self.attribution_accuracy,
            "hallucination_rate": self.hallucination_rate,
            "avg_faithfulness": self.avg_faithfulness,
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "intent_ok": r.intent_ok,
                    "metrics_ok": r.metrics_ok,
                    "skills_ok": r.skills_ok,
                    "time_ok": r.time_ok,
                    "attribution_ok": r.attribution_ok,
                    "judge": r.judge.to_dict() if r.judge else None,
                }
                for r in self.results
            ],
        }


def compute_metrics(results: List[EvalCaseResult]) -> EvalReport:
    """从逐用例结果聚合端到端指标。空输入返回全 0 报告。"""
    n = len(results)
    if n == 0:
        return EvalReport()
    passed = sum(1 for r in results if r.passed)
    six = sum(1 for r in results if r.intent_ok and r.metrics_ok and r.skills_ok)
    intent = sum(1 for r in results if r.intent_ok)
    metrics = sum(1 for r in results if r.metrics_ok)
    skills = sum(1 for r in results if r.skills_ok)
    toa_list = [r for r in results if not r.time_ok or r.time_ok]  # 全部计入
    toa = sum(1 for r in toa_list if r.time_ok)
    # 仅归因类用例计入归因准确率
    attr_cases = [r for r in results if r.is_attribution]
    attr = sum(1 for r in attr_cases if r.attribution_ok)
    # judge 指标
    judged = [r.judge for r in results if r.judge is not None]
    halluc = sum(1 for j in judged if getattr(j, "hallucination", False))
    faith = sum(getattr(j, "faithfulness", 0.0) for j in judged)

    return EvalReport(
        total=n,
        passed=passed,
        tcr=round(passed / n, 4),
        six_element_accuracy=round(six / n, 4),
        intent_accuracy=round(intent / n, 4),
        metrics_accuracy=round(metrics / n, 4),
        skills_accuracy=round(skills / n, 4),
        toa_accuracy=round(toa / n, 4),
        attribution_accuracy=round(attr / len(attr_cases), 4) if attr_cases else 0.0,
        hallucination_rate=round(halluc / len(judged), 4) if judged else 0.0,
        avg_faithfulness=round(faith / len(judged), 4) if judged else 0.0,
        results=results,
    )
