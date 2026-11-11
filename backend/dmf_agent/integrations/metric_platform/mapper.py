"""HM 元数据对象 -> v2 MetricData 映射。

迁移自老系统 metadata/hm_meta/hm_metric.py 的 HmMetricObject / check_metric_dict：
HM 元数据接口返回的指标对象字段为驼峰（id / cnName / businessMeaning / metricsType /
unit / dimensions），映射到 v2 metric_definitions 表。
"""

from __future__ import annotations

from typing import Any, Dict

from dmf_agent.schemas.metrics import MetricData

# 老系统 metricsType 枚举：1 原子指标 / 2 派生指标 / 3 衍生指标 / 4 中间派生指标
METRICS_TYPE_LABELS: Dict[int, str] = {
    1: "原子指标",
    2: "派生指标",
    3: "衍生指标",
    4: "中间派生指标",
}


def hm_metric_to_data(raw: Dict[str, Any]) -> MetricData:
    """HM 指标对象 -> MetricData。

    code 取指标 id（字符串化，主键）；category 落指标类型标签（v2 无独立
    metrics_type 列，Phase 3 若 DSL 需要精确类型可再扩展 schema）。
    """
    dims = raw.get("dimensions") or []
    return MetricData(
        code=str(raw.get("id") or ""),
        name=raw.get("cnName") or "",
        description=raw.get("businessMeaning") or "",
        unit=raw.get("unit") or "",
        category=METRICS_TYPE_LABELS.get(raw.get("metricsType"), ""),
        dimensions=[d.get("cnName") for d in dims if d.get("cnName")],
    )
