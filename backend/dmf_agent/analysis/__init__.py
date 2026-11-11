"""结果集分析（统计量 / 描述性统计 / 分布分桶），与取数后端解耦。"""

from dmf_agent.analysis.distribution import compute_distribution, format_distribution
from dmf_agent.analysis.stats import compute_stats, describe_data

__all__ = ["compute_stats", "describe_data", "compute_distribution", "format_distribution"]
