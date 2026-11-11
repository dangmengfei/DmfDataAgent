"""归因子模块（诊断三层下钻的 v2 实现）。

迁移自老 plugin/diagnosis/：
- core.py：纯 Python 归因计算（主指标概况 / 维度值归因 / 贡献 TOP / 规则总结）
- 归因图节点在 agents/metric_agent/nodes/diagnose.py（与节点体系一致）
"""

from dmf_agent.diagnosis.core import (
    contribution_top,
    dim_attribution,
    global_summary,
    metric_change,
    rows_to_dicts,
    smart_summary,
)

__all__ = [
    "contribution_top",
    "dim_attribution",
    "global_summary",
    "metric_change",
    "rows_to_dicts",
    "smart_summary",
]
