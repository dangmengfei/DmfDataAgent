"""基础统计与分布模型的包装（analysis_stats / analysis_distribution，2026-11-18 P1）。

把既有的 compute_stats/describe_data 与 compute_distribution 包装成统一契约的分析模型，
行为与原函数保持一致（「分布」触发迁到 analyze_data 的关键词规则表，语义不变）。
"""

from __future__ import annotations

from typing import Any, Dict

from dmf_agent.analysis.base import AnalysisResult, AnalysisModel, register_model
from dmf_agent.analysis.distribution import compute_distribution, format_distribution
from dmf_agent.analysis.stats import compute_stats, describe_data


class StatsModel(AnalysisModel):
    name = "analysis_stats"
    description = (
        "基础统计：对结果集各数值列计算 total/avg/max/min 与描述统计（count/mean/std/cv）。"
        "适用于「总体情况/平均多少/波动大吗」类兜底问题；也是其它模型的默认补充。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        stats = compute_stats(query_result)
        desc = describe_data(query_result)
        if not stats:
            return AnalysisResult(
                ok=False, model=self.name, summary="", error="结果集中无可分析的数值列"
            )
        parts = [
            f"{name}：均值 {s['avg']}，最高 {s['max']}，最低 {s['min']}，合计 {s['total']}"
            for name, s in stats.items()
        ]
        summary = "统计摘要：" + "；".join(parts) + "。"
        insight_lines = [summary]
        high_cv = [
            (name, d["cv"]) for name, d in desc.items() if d.get("cv", 0) >= 0.5
        ]
        for name, cv in sorted(high_cv, key=lambda kv: kv[1], reverse=True)[:3]:
            insight_lines.append(f"{name} 波动较大（变异系数 {cv}），值得进一步拆解。")
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=summary,
            data={"type": "stats", "stats": stats, "describe": desc},
            insight_lines=insight_lines,
            warnings=[],
        )


stats_model = StatsModel()
register_model(stats_model)


class DistributionModel(AnalysisModel):
    name = "analysis_distribution"
    description = (
        "分布分析：对指标在维度成员上的取值做等宽分桶计数（直方图）。"
        "适用于「取值分布/集中在哪个区间」类问题；需要 ≥3 行数据。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "目标数值列名；缺省取首个数值列"},
            "bins": {"type": "integer", "description": "分桶数（默认 8）"},
        },
    }

    def analyze(self, query_result: Dict[str, Any], params: Dict[str, Any]) -> AnalysisResult:
        metric = str(params.get("metric") or "") or None
        bins = int(params.get("bins") or 8)
        dist = compute_distribution(query_result, metric, bins=bins)
        if dist is None:
            return AnalysisResult(
                ok=False,
                model=self.name,
                summary="",
                error="数据不足或无数值列，无法做分布分析（需要 ≥3 行有效数据）",
            )
        text = format_distribution(dist)
        return AnalysisResult(
            ok=True,
            model=self.name,
            summary=text,
            data={"type": "distribution", **dist},
            insight_lines=[text],
            warnings=[],
        )


distribution_model = DistributionModel()
register_model(distribution_model)
