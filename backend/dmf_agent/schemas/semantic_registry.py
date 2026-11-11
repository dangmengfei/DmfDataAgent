"""语义注册中心持久化模型（P1 骨架）。

对应 `doc/语义层注册中心与指标权限治理方案.md` §3.1 数据模型：
- `*_key` 为**不可变稳定身份**（权限 / 血缘 / 审计 / 保存查询一律挂 key）；
- `name` / `aliases` 为可变的业务展示名（改名不断引用）；
- 状态机 `draft → review → published → deprecated`（P1 种子导入即 published，
  状态机流转在 P2 落地）；
- 一期 `scope` 只有 `industry`，与黑话词典对齐便于二期扩租户。

与 `semantic/manifest.yml` 的关系（P1 阶段）：
- manifest 仍是**运行时真源**；注册中心是"可治理的镜像"（导入种子）；
- P3 切换为 DB 真源 + 热更新，manifest 降级为导入种子 / 导出产物 / 灾备。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------- 作用域 ----------
REG_SCOPE_INDUSTRY = "industry"
REG_ALL_SCOPES = (REG_SCOPE_INDUSTRY,)

# ---------- 状态机（P1 只用 published；draft/review/deprecated 为 P2 预留） ----------
REG_STATUS_DRAFT = "draft"
REG_STATUS_REVIEW = "review"
REG_STATUS_PUBLISHED = "published"
REG_STATUS_DEPRECATED = "deprecated"
REG_ALL_STATUSES = (
    REG_STATUS_DRAFT,
    REG_STATUS_REVIEW,
    REG_STATUS_PUBLISHED,
    REG_STATUS_DEPRECATED,
)
REG_STATUS_LABELS = {
    REG_STATUS_DRAFT: "草稿",
    REG_STATUS_REVIEW: "待审核",
    REG_STATUS_PUBLISHED: "已发布",
    REG_STATUS_DEPRECATED: "已废弃",
}

# ---------- 来源 ----------
REG_SOURCE_BUILTIN = "builtin"  # manifest.yml 种子导入
REG_SOURCE_IMPORTED = "imported"  # 批量导入
REG_SOURCE_MANUAL = "manual"  # 管理面手工创建（P2+）
REG_ALL_SOURCES = (REG_SOURCE_BUILTIN, REG_SOURCE_IMPORTED, REG_SOURCE_MANUAL)

# ---------- 指标类型 ----------
REG_METRIC_SIMPLE = "simple"
REG_METRIC_RATIO = "ratio"
REG_METRIC_DERIVED = "derived"
REG_ALL_METRIC_TYPES = (REG_METRIC_SIMPLE, REG_METRIC_RATIO, REG_METRIC_DERIVED)
REG_METRIC_TYPE_LABELS = {
    REG_METRIC_SIMPLE: "原子指标",
    REG_METRIC_RATIO: "派生指标",
    REG_METRIC_DERIVED: "衍生指标",
}


class SemanticEntityData(BaseModel):
    name: str
    entity_type: str = "foreign"
    expr: str = ""

    def to_cache(self) -> Dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_cache(cls, raw: Optional[Dict[str, Any]]) -> "SemanticEntityData":
        return cls.model_validate(raw or {"name": "", "entity_type": "foreign", "expr": ""})


class SemanticDimensionData(BaseModel):
    dimension_key: str
    name: str
    dim_type: str = "categorical"
    expr: str = ""
    time_granularity: str = "day"
    description: str = ""
    status: str = REG_STATUS_PUBLISHED
    version: int = 1

    def to_cache(self) -> Dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_cache(cls, raw: Optional[Dict[str, Any]]) -> "SemanticDimensionData":
        return cls.model_validate(raw or {})


class SemanticMeasureData(BaseModel):
    measure_key: str
    name: str
    agg: str = "sum"
    expr: str = ""
    agg_time_dimension: str = ""
    unit: str = ""
    description: str = ""
    status: str = REG_STATUS_PUBLISHED
    version: int = 1

    def to_cache(self) -> Dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_cache(cls, raw: Optional[Dict[str, Any]]) -> "SemanticMeasureData":
        return cls.model_validate(raw or {})


class SemanticModelData(BaseModel):
    model_key: str
    name: str
    description: str = ""
    table: str = ""
    agg_time_dimension: str = ""
    entities: List[SemanticEntityData] = Field(default_factory=list)
    dimensions: List[SemanticDimensionData] = Field(default_factory=list)
    measures: List[SemanticMeasureData] = Field(default_factory=list)
    status: str = REG_STATUS_PUBLISHED
    version: int = 1
    source: str = REG_SOURCE_BUILTIN
    owner: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_cache(self) -> Dict[str, Any]:
        d = self.model_dump()
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def from_cache(cls, raw: Optional[Dict[str, Any]]) -> "SemanticModelData":
        if not raw:
            return cls(model_key="", name="")
        data = dict(raw)
        data["entities"] = [SemanticEntityData.from_cache(e) for e in data.get("entities") or []]
        data["dimensions"] = [
            SemanticDimensionData.from_cache(x) for x in data.get("dimensions") or []
        ]
        data["measures"] = [SemanticMeasureData.from_cache(x) for x in data.get("measures") or []]
        return cls.model_validate(data)


class SemanticMetricData(BaseModel):
    metric_key: str
    name: str
    metric_type: str = REG_METRIC_SIMPLE
    type_params: Dict[str, Any] = Field(default_factory=dict)
    aliases: List[str] = Field(default_factory=list)
    unit: str = ""
    description: str = ""
    scale: float = 1.0
    round: int = 4
    granularities: List[str] = Field(default_factory=list)
    pop_types: List[str] = Field(default_factory=list)
    owner: str = ""
    status: str = REG_STATUS_PUBLISHED
    version: int = 1
    source: str = REG_SOURCE_BUILTIN
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_cache(self) -> Dict[str, Any]:
        d = self.model_dump()
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def from_cache(cls, raw: Optional[Dict[str, Any]]) -> "SemanticMetricData":
        if not raw:
            return cls(metric_key="", name="")
        return cls.model_validate(raw)


class RegistryStats(BaseModel):
    """注册中心统计卡（模型 / 指标 / 维度 / 度量 / 状态分布）。"""

    models: int = 0
    metrics: int = 0
    dimensions: int = 0
    measures: int = 0
    metrics_by_status: Dict[str, int] = Field(default_factory=dict)
    metrics_by_type: Dict[str, int] = Field(default_factory=dict)
    models_by_status: Dict[str, int] = Field(default_factory=dict)


class RegistryFacets(BaseModel):
    """过滤选项（管理页下拉）。"""

    metric_types: List[str] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)
    model_keys: List[str] = Field(default_factory=list)
    units: List[str] = Field(default_factory=list)


class RegistryPage(BaseModel):
    """分页容器（与 slang 管理接口一致的分页契约）。"""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
