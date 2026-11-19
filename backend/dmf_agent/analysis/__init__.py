"""结果集分析（统计量 / 描述性统计 / 分布分桶 + 分析模型工具库），与取数后端解耦。

2026-11-18 数据分析能力提升 P1：新增统一契约（base）与分析模型工具库
（trend/anomaly/pareto/compare/correlation + stats/distribution 包装 + tools 执行器），
见 doc/数据分析能力提升方案.md。
"""

from dmf_agent.analysis.base import (
    AnalysisModel,
    AnalysisResult,
    MODEL_REGISTRY,
    get_model,
    model_names,
    numeric_columns,
    column_values,
    register_model,
)
from dmf_agent.analysis.distribution import compute_distribution, format_distribution
from dmf_agent.analysis.stats import compute_stats, describe_data, is_time_column
from dmf_agent.analysis.tools import keyword_plan, run_analysis, run_analysis_tool

# 模型模块导入即注册（MODEL_REGISTRY 副作用）；新增模型在此追加一行
from dmf_agent.analysis import basic_models  # noqa: F401  (analysis_stats / analysis_distribution)
from dmf_agent.analysis import trend  # noqa: F401  (analysis_trend)
from dmf_agent.analysis import anomaly  # noqa: F401  (analysis_anomaly)
from dmf_agent.analysis import pareto  # noqa: F401  (analysis_pareto)
from dmf_agent.analysis import compare  # noqa: F401  (analysis_compare)
from dmf_agent.analysis import correlation  # noqa: F401  (analysis_correlation)

__all__ = [
    "compute_stats",
    "describe_data",
    "compute_distribution",
    "format_distribution",
    "is_time_column",
    "AnalysisResult",
    "AnalysisModel",
    "MODEL_REGISTRY",
    "get_model",
    "model_names",
    "register_model",
    "numeric_columns",
    "column_values",
    "run_analysis",
    "run_analysis_tool",
    "keyword_plan",
]
