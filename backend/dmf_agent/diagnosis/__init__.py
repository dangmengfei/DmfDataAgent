"""归因子模块（诊断三层下钻的 v2 实现）。

迁移自老 plugin/diagnosis/：
- core.py：纯 Python 归因计算（主指标概况 / 维度值归因 / 贡献 TOP / 规则总结）
  + 期间对比（2026-11-12 P1：period_change / period_dim_attribution /
  period_global_summary / period_smart_summary）
- anomaly.py：异动前置判定（significant / mild / noise / insufficient 四态）
- 归因图节点在 agents/metric_agent/nodes/diagnose.py（与节点体系一致）
"""

from dmf_agent.diagnosis.anomaly import detect_anomaly
from dmf_agent.diagnosis.core import (
    contribution_top,
    cross_attribution,
    dim_attribution,
    global_summary,
    metric_change,
    period_change,
    period_dim_attribution,
    period_global_summary,
    period_smart_summary,
    rows_to_dicts,
    smart_summary,
)

__all__ = [
    "contribution_top",
    "cross_attribution",
    "dim_attribution",
    "global_summary",
    "metric_change",
    "period_change",
    "period_dim_attribution",
    "period_global_summary",
    "period_smart_summary",
    "rows_to_dicts",
    "smart_summary",
    "detect_anomaly",
]
