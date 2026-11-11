"""指标元数据模型（迁移自老系统 metadata/hm_meta 指标定义）。"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel


class MetricData(BaseModel):
    code: str  # 指标编码
    name: str  # 指标中文名
    description: str = ""
    unit: str = ""
    category: str = ""
    # 该指标可用维度编码列表
    dimensions: List[str] = []
