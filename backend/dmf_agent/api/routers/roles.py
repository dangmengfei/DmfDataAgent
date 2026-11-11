"""角色管理接口（仅管理员）：内置角色展示 + 自定义角色增删改 + 权限点清单。

- `GET /roles` 列表读 roles 表（DB 空时回落静态内置三级，降级可用）；
- `POST /roles` 新增自定义角色（name 唯一、`[a-z0-9_]{2,32}`、权限点 ⊆ ALL_PERMISSIONS，
  通配符 `*` 仅内置 admin 持有，自定义角色不可声明）；
- `PUT /roles/{name}` 修改；**内置角色仅允许改 label/description 与启停，权限拒改**
  （admin 的 `*` 与 analyst/viewer 基线防锁死/意外提权）；
- `DELETE /roles/{name}` 内置角色拒删；仍被用户引用的自定义角色拒删（先迁移用户）。

角色权限调整后已签发令牌不回改，refresh 续期或重新登录生效（与既有约定一致）。
"""

from __future__ import annotations

import logging
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_role
from dmf_agent.auth.roles import (
    ADMIN,
    ALL_PERMISSIONS,
    is_builtin_role_name,
    list_roles,
)
from dmf_agent.schemas.auth import RoleCreate, RoleInfo, RoleUpdate
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/roles", tags=["roles"], dependencies=[Depends(require_role(ADMIN))]
)

_WILDCARD = "*"
_NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def _to_info(role) -> RoleInfo:
    return RoleInfo(
        name=role.name,
        label=role.label,
        description=role.description,
        permissions=list(role.permissions or []),
        status=role.status,
        is_builtin=bool(role.is_builtin),
        created_at=role.created_at,
    )


async def _list_from_repo_or_static(runtime: AppRuntime) -> List[RoleInfo]:
    """列表：roles 表优先；DB 异常或空表回落静态内置三级（降级矩阵）。"""
    try:
        rows = await runtime.db.role_repo.list_all()
    except Exception:  # noqa: BLE001 - DB 故障回落静态
        rows = []
    if rows:
        return [_to_info(r) for r in rows]
    return list_roles()


@router.get("", response_model=List[RoleInfo], summary="角色列表（含自定义）")
async def get_roles(runtime: AppRuntime = Depends(get_runtime)) -> List[RoleInfo]:
    return await _list_from_repo_or_static(runtime)


@router.get("/permissions", response_model=List[str], summary="全部可分配权限点")
async def get_permission_catalog() -> List[str]:
    return list(ALL_PERMISSIONS)


def _validate_permissions(permissions: List[str]) -> None:
    if _WILDCARD in permissions:
        raise HTTPException(
            status_code=400, detail="通配符 * 仅内置管理员角色持有，自定义角色不可声明"
        )
    unknown = [p for p in permissions if p not in ALL_PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知权限点：{', '.join(unknown)}")


@router.post("", response_model=RoleInfo, summary="新增角色")
async def create_role(
    body: RoleCreate, runtime: AppRuntime = Depends(get_runtime)
) -> RoleInfo:
    if not _NAME_RE.match(body.name):
        raise HTTPException(
            status_code=400, detail="角色标识仅允许小写字母/数字/下划线，2~32 位"
        )
    if is_builtin_role_name(body.name) or await runtime.db.role_repo.get(body.name):
        raise HTTPException(status_code=409, detail=f"角色已存在：{body.name}")
    _validate_permissions(body.permissions)

    from dmf_agent.schemas.auth import RoleData

    saved = await runtime.db.role_repo.add(
        RoleData(
            name=body.name,
            label=body.label,
            description=body.description,
            permissions=body.permissions,
            status="active",
            is_builtin=False,
        )
    )
    logger.info("新增角色：%s（%s 个权限点）", body.name, len(body.permissions))
    return _to_info(saved)


@router.put("/{name}", response_model=RoleInfo, summary="修改角色（信息 / 权限 / 启停）")
async def update_role(
    name: str, body: RoleUpdate, runtime: AppRuntime = Depends(get_runtime)
) -> RoleInfo:
    role = await runtime.db.role_repo.get(name)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    if role.is_builtin or is_builtin_role_name(name):
        # 内置角色防锁死：仅允许改展示信息与启停，权限一律拒改
        if body.permissions is not None:
            raise HTTPException(
                status_code=409, detail="内置角色的权限不可修改（防止权限基线被意外改坏）"
            )
        if name == ADMIN and body.status == "disabled":
            raise HTTPException(status_code=409, detail="管理员角色不可停用")
    elif body.permissions is not None:
        _validate_permissions(body.permissions)

    updated = await runtime.db.role_repo.update(
        name,
        label=body.label,
        description=body.description,
        permissions=body.permissions,
        status=body.status,
    )
    logger.info("修改角色：%s（permissions_changed=%s）", name, body.permissions is not None)
    return _to_info(updated)


@router.delete("/{name}", summary="删除自定义角色")
async def delete_role(name: str, runtime: AppRuntime = Depends(get_runtime)) -> dict:
    if is_builtin_role_name(name):
        raise HTTPException(status_code=409, detail="内置角色不可删除")
    role = await runtime.db.role_repo.get(name)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.is_builtin:
        raise HTTPException(status_code=409, detail="内置角色不可删除")
    # 引用检查：仍被用户持有则拒绝，先在用户管理中迁移这些用户
    holders = [u.username for u in await runtime.db.user_repo.list_all() if u.role == name]
    if holders:
        raise HTTPException(
            status_code=409,
            detail=f"仍有 {len(holders)} 个用户使用该角色（{'、'.join(holders[:5])}），请先迁移",
        )
    await runtime.db.role_repo.delete(name)
    logger.info("删除角色：%s", name)
    return {"ok": True, "deleted": name}
