"""评测用例（迁移自老系统 sa_utils/tuner.py + auto_test*.py 的回归问题集）。

v2 以意图 + 要素（指标/技能）为断言维度，mock 模式下可离线回归；
真实 LLM 配置后同一套用例做准确率回归（Phase 1 遗留的意图回归在此持续化）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class EvalCase:
    name: str
    query: str
    expected_intent: str
    expected_metrics: List[str] = field(default_factory=list)
    expected_skills: List[str] = field(default_factory=list)


DEFAULT_CASES: List[EvalCase] = [
    EvalCase(name="metric_simple", query="查一下近7天的GMV趋势",
             expected_intent="metric_query", expected_metrics=["GMV"]),
    EvalCase(name="metric_with_skill", query="查一下近7天的GMV趋势，画个图",
             expected_intent="metric_query", expected_metrics=["GMV"],
             expected_skills=["chart_visualization"]),
    EvalCase(name="diagnosis", query="近7天GMV为什么下降了，帮我做个归因分析",
             expected_intent="diagnosis", expected_metrics=["GMV"]),
    EvalCase(name="knowledge", query="什么是北极星指标，文档里有说明吗",
             expected_intent="knowledge_qa"),
    EvalCase(name="dimension", query="北京有哪些门店",
             expected_intent="dimension_query"),
    EvalCase(name="chitchat", query="你好呀",
             expected_intent="chitchat"),
]
