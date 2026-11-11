"""FR13 授权（enforce）Pydantic schema。

对应 `db/models.py` 的 `MetricGrantModel` / `DataScopeModel` / `DimensionPolicyModel`。
落库形态与 ORM 行互转；管理面接口直接消费这些 Data。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class MetricGrantData(BaseModel):
    """指标授权（白名单语义：有记录的主体只可见被授予的指标）。"""

    id: Optional[int] = None
    tenant_id: str = ""
    user_id: str = ""  # 与 role 二选一
    role: str = ""
    metric_key: str  # 稳定键（与 manifest.yml 一致）
    granted_by: str = ""
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_row(cls, r: Any) -> "MetricGrantData":
        return cls(
            id=getattr(r, "id", None),
            tenant_id=r.tenant_id or "",
            user_id=r.user_id or "",
            role=r.role or "",
            metric_key=r.metric_key,
            granted_by=r.granted_by or "",
            created_at=r.created_at,
        )


class DataScopeData(BaseModel):
    """行权限：主体只可见某维度的某些值。"""

    id: Optional[int] = None
    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    dimension: str  # 维度别名
    allowed_values: List[str] = Field(default_factory=list)
    granted_by: str = ""
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_row(cls, r: Any) -> "DataScopeData":
        raw = getattr(r, "allowed_values", None) or []
        return cls(
            id=getattr(r, "id", None),
            tenant_id=r.tenant_id or "",
            user_id=r.user_id or "",
            role=r.role or "",
            dimension=r.dimension,
            allowed_values=list(raw),
            granted_by=r.granted_by or "",
            created_at=r.created_at,
        )


class DimensionPolicyData(BaseModel):
    """列/维度 deny 策略：主体被禁止查看某些维度列（结果中硬剔除）。"""

    id: Optional[int] = None
    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    dimension: str
    denied: bool = True
    granted_by: str = ""
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_row(cls, r: Any) -> "DimensionPolicyData":
        return cls(
            id=getattr(r, "id", None),
            tenant_id=r.tenant_id or "",
            user_id=r.user_id or "",
            role=r.role or "",
            dimension=r.dimension,
            denied=bool(getattr(r, "denied", True)),
            granted_by=r.granted_by or "",
            created_at=r.created_at,
        )
