"""FR13 授权加载：从授权仓储加载用户/角色的权限上下文。

把 DB 里的 `metric_grants` / `data_scopes` / `dimension_policies` 三张表，
按 (tenant_id, user_id, role) 合并成一个 `UserPermissionContext`，供 enforce 引擎与审计使用。

合并语义（与仓储 `list_for` 一致）：
- **指标白名单**：只要该主体有任何 grant 记录，才限制（allowed_metrics = 并集）；
  无任何记录 = 不限制（可见全部指标，向后兼容）。
- **行权限**：按维度聚合 allowed_values（set 合并）。
- **列 deny**：denied=True 的维度进 denied_dimensions（结果中硬剔除）。

任何仓储不可用 / 抛错都安全降级为"空授权"（不限制），保证授权存储故障不阻断取数。
"""

from __future__ import annotations

from typing import List, Optional

from dmf_agent.db.base import (
    DataScopeRepository,
    DimensionPolicyRepository,
    MetricGrantRepository,
)
from dmf_agent.schemas.permission import (
    DataScopeData,
    DimensionPolicyData,
    MetricGrantData,
)
from dmf_agent.security.permissions import UserPermissionContext


async def load_permission_context(
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    grant_repo: Optional[MetricGrantRepository],
    scope_repo: Optional[DataScopeRepository],
    policy_repo: Optional[DimensionPolicyRepository],
) -> UserPermissionContext:
    ctx = UserPermissionContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=[role] if role else [],
    )

    grants: List[MetricGrantData] = []
    scopes: List[DataScopeData] = []
    policies: List[DimensionPolicyData] = []

    if grant_repo is not None:
        try:
            grants = await grant_repo.list_for(
                tenant_id=tenant_id, user_id=user_id, role=role
            )
        except Exception:  # noqa: BLE001 - 授权存储故障降级为"不限制"
            grants = []
    if scope_repo is not None:
        try:
            scopes = await scope_repo.list_for(
                tenant_id=tenant_id, user_id=user_id, role=role
            )
        except Exception:  # noqa: BLE001
            scopes = []
    if policy_repo is not None:
        try:
            policies = await policy_repo.list_for(
                tenant_id=tenant_id, user_id=user_id, role=role
            )
        except Exception:  # noqa: BLE001
            policies = []

    # 指标白名单：有记录才限制；空记录保持默认（不限制）
    if grants:
        ctx.allowed_metrics = {g.metric_key for g in grants}

    # 行权限：按维度聚合允许值
    for s in scopes:
        ctx.row_scopes.setdefault(s.dimension, set()).update(s.allowed_values or [])

    # 列 deny：denied=True 的维度进 denied_dimensions
    for p in policies:
        if p.denied:
            ctx.denied_dimensions.add(p.dimension)

    return ctx
