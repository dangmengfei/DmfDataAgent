"""FR13 权限管理 API（admin）：指标授权 / 行权限 / 列 deny 的授予、撤销与查看。

所有接口要求 `admin` 权限点（admin 通配放行；鉴权关闭时合成 admin 亦放行）。
授权存储是 enforce 的数据来源：空表 = 不限制；有记录才约束。
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_ADMIN
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.permission import (
    DataScopeData,
    DimensionPolicyData,
    MetricGrantData,
)
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()


class GrantMetricIn(BaseModel):
    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    metric_key: str


class ScopeIn(BaseModel):
    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    dimension: str
    allowed_values: List[str] = []


class PolicyIn(BaseModel):
    tenant_id: str = ""
    user_id: str = ""
    role: str = ""
    dimension: str
    denied: bool = True


# ---------------- 指标授权（白名单） ----------------

@router.post("/permission/grants/metric", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def grant_metric(
    in_: GrantMetricIn,
    current: CurrentUser = Depends(require_permission(PERM_ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    saved = await runtime.db.metric_grant_repo.add(
        MetricGrantData(
            tenant_id=in_.tenant_id,
            user_id=in_.user_id,
            role=in_.role,
            metric_key=in_.metric_key,
            granted_by=current.username,
        )
    )
    return saved.model_dump()


@router.delete("/permission/grants/metric", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def revoke_metric(
    metric_key: str = Query(...),
    tenant_id: str = "",
    user_id: str = "",
    role: str = "",
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    ok = await runtime.db.metric_grant_repo.delete(
        tenant_id=tenant_id, user_id=user_id, role=role, metric_key=metric_key
    )
    return {"deleted": bool(ok)}


@router.get("/permission/grants", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def list_grants(
    tenant_id: str = "", limit: int = 500, runtime: AppRuntime = Depends(get_runtime)
) -> dict:
    rows = await runtime.db.metric_grant_repo.list_all(tenant_id=tenant_id, limit=limit)
    return {"items": [r.model_dump() for r in rows], "total": len(rows)}


# ---------------- 行权限（下推到 SQL WHERE） ----------------

@router.post("/permission/scopes", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def set_scope(
    in_: ScopeIn,
    current: CurrentUser = Depends(require_permission(PERM_ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    saved = await runtime.db.data_scope_repo.add(
        DataScopeData(
            tenant_id=in_.tenant_id,
            user_id=in_.user_id,
            role=in_.role,
            dimension=in_.dimension,
            allowed_values=list(in_.allowed_values or []),
            granted_by=current.username,
        )
    )
    return saved.model_dump()


@router.delete("/permission/scopes", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def revoke_scope(
    dimension: str = Query(...),
    tenant_id: str = "",
    user_id: str = "",
    role: str = "",
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    ok = await runtime.db.data_scope_repo.delete(
        tenant_id=tenant_id, user_id=user_id, role=role, dimension=dimension
    )
    return {"deleted": bool(ok)}


@router.get("/permission/scopes", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def list_scopes(
    tenant_id: str = "", limit: int = 500, runtime: AppRuntime = Depends(get_runtime)
) -> dict:
    rows = await runtime.db.data_scope_repo.list_all(tenant_id=tenant_id, limit=limit)
    return {"items": [r.model_dump() for r in rows], "total": len(rows)}


# ---------------- 列 / 维度 deny（结果集硬剔除） ----------------

@router.post("/permission/dimension-policies", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def set_policy(
    in_: PolicyIn,
    current: CurrentUser = Depends(require_permission(PERM_ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    saved = await runtime.db.dimension_policy_repo.add(
        DimensionPolicyData(
            tenant_id=in_.tenant_id,
            user_id=in_.user_id,
            role=in_.role,
            dimension=in_.dimension,
            denied=in_.denied,
            granted_by=current.username,
        )
    )
    return saved.model_dump()


@router.delete("/permission/dimension-policies", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def revoke_policy(
    dimension: str = Query(...),
    tenant_id: str = "",
    user_id: str = "",
    role: str = "",
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    ok = await runtime.db.dimension_policy_repo.delete(
        tenant_id=tenant_id, user_id=user_id, role=role, dimension=dimension
    )
    return {"deleted": bool(ok)}


@router.get("/permission/dimension-policies", dependencies=[Depends(require_permission(PERM_ADMIN))])
async def list_policies(
    tenant_id: str = "", limit: int = 500, runtime: AppRuntime = Depends(get_runtime)
) -> dict:
    rows = await runtime.db.dimension_policy_repo.list_all(tenant_id=tenant_id, limit=limit)
    return {"items": [r.model_dump() for r in rows], "total": len(rows)}


# ---------------- 当前用户的有效权限上下文（T198f 前端权限标记的数据源） ----------------

@router.get(
    "/permission/my-context",
    summary="当前登录用户的有效权限上下文（供前端显隐/标记受限的指标与维度）",
)
async def my_permission_context(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """返回**调用者自己**的有效授权（不是管理面全量），供前端做「权限标记 / 显隐」。

    注意本接口**不加 admin 守卫** —— 每个登录用户都该知道"哪些东西我看不到"，
    否则界面上"指标突然查不出数"会被当成 bug。返回的是收敛后的结论，不泄露
    其他主体的授权矩阵。

    `mode` 语义（与 `PERMISSION_MODE` 一致）：off=未启用 / audit=只审计不拦截 /
    enforce=真正拦截。`unrestricted_metrics=True` 表示无白名单记录 = 不限制（向后兼容）。
    """
    coord = getattr(runtime, "security", None)
    if coord is None:
        return {
            "mode": "off",
            "user_id": current.id,
            "role": current.role,
            "tenant_id": current.tenant_id,
            "restricted": False,
            "unrestricted_metrics": True,
            "allowed_metrics": [],
            "denied_dimensions": [],
            "row_scope_dimensions": [],
            "row_scope_values": {},
        }
    ctx = await coord.build_permission_context(
        user_id=current.id, tenant_id=current.tenant_id, role=current.role
    )
    return {
        "mode": coord.permission_mode,
        "user_id": current.id,
        "role": current.role,
        "tenant_id": current.tenant_id,
        "restricted": bool(ctx.allowed_metrics or ctx.denied_dimensions or ctx.row_scopes),
        # 无白名单记录 = 不限制（与 policy_loader / filter_metrics 的语义完全一致）
        "unrestricted_metrics": not ctx.allowed_metrics,
        "allowed_metrics": sorted(ctx.allowed_metrics),
        "denied_dimensions": sorted(ctx.denied_dimensions),
        "row_scope_dimensions": sorted(ctx.row_scopes.keys()),
        "row_scope_values": {k: sorted(v) for k, v in ctx.row_scopes.items()},
    }
