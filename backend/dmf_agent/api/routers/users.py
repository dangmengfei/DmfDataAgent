"""用户管理接口（仅管理员）：增删改查 / 重置密码 / 启停。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_role
from dmf_agent.auth.roles import is_valid_role
from dmf_agent.auth.security import hash_password
from dmf_agent.schemas.auth import (
    CurrentUser,
    PasswordChange,
    UserCreate,
    UserData,
    UserUpdate,
)
from dmf_agent.services.runtime import AppRuntime

router = APIRouter(
    prefix="/users", tags=["users"], dependencies=[Depends(require_role("admin"))]
)


async def _fill_tenant_code(runtime: AppRuntime, user: UserData) -> UserData:
    tenant = await runtime.db.tenant_repo.get(user.tenant_id)
    user.tenant_code = tenant.code if tenant else ""
    return user


async def _assert_role_exists(runtime: AppRuntime, role_name: str) -> None:
    """角色合法性：内置静态 ∪ roles 表（自定义角色）；已禁用同样拒绝。"""
    if is_valid_role(role_name):
        return
    role = await runtime.db.role_repo.get(role_name)
    if role is not None and role.status == "active":
        return
    raise HTTPException(status_code=400, detail=f"非法角色：{role_name}")


@router.get("", response_model=List[UserData], summary="用户列表")
async def list_users(
    tenant_id: Optional[str] = None,
    runtime: AppRuntime = Depends(get_runtime),
) -> List[UserData]:
    users = await runtime.db.user_repo.list_all(tenant_id=tenant_id)
    return [await _fill_tenant_code(runtime, u) for u in users]


@router.post("", response_model=UserData, summary="新建用户")
async def create_user(
    body: UserCreate, runtime: AppRuntime = Depends(get_runtime)
) -> UserData:
    auth = runtime.settings.auth
    if len(body.password) < auth.password_min_length:
        raise HTTPException(status_code=400, detail=f"密码长度至少 {auth.password_min_length} 位")
    await _assert_role_exists(runtime, body.role)
    if await runtime.db.user_repo.get_by_username(body.username):
        raise HTTPException(status_code=409, detail=f"用户名已存在：{body.username}")
    if await runtime.db.tenant_repo.get(body.tenant_id) is None:
        raise HTTPException(status_code=400, detail="所属租户不存在")

    user = await runtime.db.user_repo.add(
        UserData(
            id="",
            username=body.username,
            display_name=body.display_name,
            tenant_id=body.tenant_id,
            role=body.role,
        ),
        hash_password(body.password),
    )
    return await _fill_tenant_code(runtime, user)


@router.patch("/{user_id}", response_model=UserData, summary="更新用户")
async def update_user(
    user_id: str, body: UserUpdate, runtime: AppRuntime = Depends(get_runtime)
) -> UserData:
    if body.role is not None:
        await _assert_role_exists(runtime, body.role)
    if body.tenant_id is not None and await runtime.db.tenant_repo.get(body.tenant_id) is None:
        raise HTTPException(status_code=400, detail="所属租户不存在")
    user = await runtime.db.user_repo.update(
        user_id,
        display_name=body.display_name,
        tenant_id=body.tenant_id,
        role=body.role,
        status=body.status,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return await _fill_tenant_code(runtime, user)


@router.post("/{user_id}/password", summary="重置用户密码（管理员）")
async def reset_password(
    user_id: str,
    body: PasswordChange,
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    min_len = runtime.settings.auth.password_min_length
    if len(body.new_password) < min_len:
        raise HTTPException(status_code=400, detail=f"密码长度至少 {min_len} 位")
    ok = await runtime.db.user_repo.set_password(user_id, hash_password(body.new_password))
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@router.delete("/{user_id}", summary="删除用户")
async def delete_user(
    user_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    deleted = await runtime.db.user_repo.delete(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"deleted": True}
