"""语义层发布快照 schema（混合方案：YAML 基座 → DB 发布快照 → 运行时装配）。

对应 `db/models.py` 的 `SemanticPublishedModel`。一条记录 = 某租户某版本的
语义定义快照；`state` 复用语义注册中心状态机
（draft → review → published → deprecated）。运行时取数默认只消费 `published`
态的最新版本；无该租户快照时回落到全局（`tenant_id=""`）。

`mode` 区分两种快照：
- ``full``    —— payload 是整份已合并 manifest；
- ``overlay`` —— payload 只含被改动的 model/metric 条目，运行时按 ``name``
  整条替换叠加到基座之上（见 `semantic/overlay.py`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SemanticPublishedData(BaseModel):
    """一条语义层发布快照（payload = 已合并 manifest 原始字典或差异条目）。"""

    id: Optional[int] = None
    tenant_id: str = ""  # "" = 全局默认（所有租户共享）
    version: int = 1  # 同租户内递增
    state: str = "published"  # draft | review | published | deprecated
    mode: str = "full"  # full | overlay
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_by: str = ""
    created_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    approved_by: str = ""
    rejected_by: str = ""
    reject_reason: str = ""

    @classmethod
    def from_orm_row(cls, r: Any) -> "SemanticPublishedData":
        raw = getattr(r, "payload", None) or {}
        return cls(
            id=getattr(r, "id", None),
            tenant_id=r.tenant_id or "",
            version=int(getattr(r, "version", 1) or 1),
            state=r.state or "published",
            mode=getattr(r, "mode", None) or "full",
            payload=dict(raw),
            created_by=r.created_by or "",
            created_at=r.created_at,
            published_at=getattr(r, "published_at", None),
            approved_by=getattr(r, "approved_by", "") or "",
            rejected_by=getattr(r, "rejected_by", "") or "",
            reject_reason=getattr(r, "reject_reason", "") or "",
        )
