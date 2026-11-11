"""FastAPI 鉴权依赖：当前用户解析、角色守卫与权限点守卫。

- `get_current_user`：读取 `Authorization: Bearer <token>` 解析当前用户；
  只接受 `typ=access` 的令牌（refresh 令牌只能用于 `/auth/refresh`）。
  鉴权关闭（DMF_AUTH-ENABLED=false，测试环境）时返回合成 admin（放行全部，
  tenant_id 为空表示不做租户过滤，保持既有行为）。
- `require_role(*roles)`：角色守卫（admin 始终放行）；鉴权关闭时同样放行。
- `require_permission(*perms)`：**接口级**权限点守卫（命中任一即放行）；
  admin（`*`）与鉴权关闭场景放行。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.roles import (
    ADMIN,
    effective_permissions,
    permissions_grant,
    role_permissions,
    role_permissions_from,
)
from dmf_agent.auth.security import decode_token
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.services.runtime import AppRuntime

_BEARER_PREFIX = "bearer "
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _auth_disabled_user() -> CurrentUser:
    """鉴权关闭时的合成用户（admin，空租户 → 不做租户过滤）。"""
    return CurrentUser(
        id="anonymous",
        username="anonymous",
        display_name="本地用户",
        tenant_id="",
        tenant_code="",
        role=ADMIN,
        permissions=role_permissions(ADMIN),
    )


async def get_current_user(
    request: Request,
    runtime: AppRuntime = Depends(get_runtime),
) -> CurrentUser:
    if not runtime.settings.auth.enabled:
        return _auth_disabled_user()

    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith(_BEARER_PREFIX):
        raise HTTPException(status_code=401, detail="未登录或缺少访问令牌", headers=_UNAUTHORIZED_HEADERS)
    token = header[len(_BEARER_PREFIX):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录或缺少访问令牌", headers=_UNAUTHORIZED_HEADERS)

    auth = runtime.settings.auth
    try:
        # 只接受 access 令牌：refresh 令牌（typ=refresh）在此被拒绝
        payload = decode_token(token, auth.jwt_secret, auth.jwt_algorithm, expected_type="access")
    except Exception:  # noqa: BLE001 - 任何解析/过期/类型不符统一 401
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期", headers=_UNAUTHORIZED_HEADERS)

    role = str(payload.get("role") or "viewer")
    # 权限展开优先读 roles 表（自定义角色 / 调整后的权限）；DB 异常或角色缺失回落静态定义
    permissions = await role_permissions_from(getattr(runtime.db, "role_repo", None), role)
    if permissions is None:
        # 角色已被禁用：令牌仍有效但身份失效，按 401 让前端走刷新/重登
        raise HTTPException(
            status_code=401,
            detail="用户角色已被停用，请重新登录",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return CurrentUser(
        id=str(payload.get("sub") or ""),
        username=str(payload.get("username") or ""),
        display_name=str(payload.get("username") or ""),
        tenant_id=str(payload.get("tenant_id") or ""),
        tenant_code=str(payload.get("tenant_code") or ""),
        role=role,
        permissions=permissions,
    )


def require_role(*roles: str):
    """角色守卫：命中任一角色或为 admin 放行；否则 403。"""
    allowed = set(roles)

    async def _dep(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role == ADMIN or current.role in allowed:
            return current
        raise HTTPException(status_code=403, detail="权限不足：需要 " + "/".join(sorted(allowed)))

    return _dep


def require_permission(*permissions: str):
    """权限点守卫（接口级）：命中任一权限点或为 admin 放行；否则 403。

    与 `require_role` 的区别：按**能力**而非角色授权，新增角色时无需改路由。

    判定口径（与 `get_current_user` / 前端 `hasPerm` 一致，勿再退回静态字典）：
    以**身份携带的 `current.permissions`**（DB 优先，见 `role_permissions_from`）为准，
    仅在缺失时回落静态角色定义。此前只读 `roles._ROLES` 静态字典，导致管理面新建的
    自定义角色永远过不了守卫——表现是"角色权限里明明勾了，接口却 403"。

    用法：`Depends(require_permission(PERM_KNOWLEDGE_WRITE))`。
    """
    wanted = list(permissions)

    async def _dep(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        # admin 角色硬放行（既有契约，与 require_role 一致；不受 DB 权限表改动影响）
        if current.role == ADMIN:
            return current
        if permissions_grant(effective_permissions(current.role, current.permissions), wanted):
            return current
        raise HTTPException(
            status_code=403, detail="权限不足：需要 " + "/".join(sorted(wanted))
        )

    return _dep
