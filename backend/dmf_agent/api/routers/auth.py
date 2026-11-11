"""认证接口：登录 / 刷新令牌 / 当前用户 / 自助改密。

登录与刷新接口无需访问令牌（鉴权入口）；其余接口在 app 装配时统一要求登录。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.auth.security import hash_password, verify_password
from dmf_agent.auth.service import AuthError, AuthService
from dmf_agent.schemas.auth import (
    CurrentUser,
    LoginRequest,
    PasswordChange,
    RefreshRequest,
    TokenResponse,
)
from dmf_agent.services.runtime import AppRuntime

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, summary="登录（签发 access + refresh）")
async def login(body: LoginRequest, runtime: AppRuntime = Depends(get_runtime)) -> TokenResponse:
    service = AuthService(runtime.db, runtime.settings)
    try:
        return await service.login(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌换新（轮换 refresh）")
async def refresh(body: RefreshRequest, runtime: AppRuntime = Depends(get_runtime)) -> TokenResponse:
    """用 refresh_token 换发新的访问令牌。

    公开接口（不带访问令牌）：访问令牌过期后前端正是靠它静默续期。
    返回体同时携带**新的** refresh_token（滚动轮换），前端应整体替换本地令牌。
    """
    if not runtime.settings.auth.enabled:
        raise HTTPException(status_code=400, detail="鉴权未启用，无需刷新令牌")
    service = AuthService(runtime.db, runtime.settings)
    try:
        return await service.refresh(body.refresh_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@router.get("/me", response_model=CurrentUser, summary="当前登录用户")
async def me(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current


@router.post("/change-password", summary="自助修改密码（需校验原密码）")
async def change_password(
    body: PasswordChange,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    if not runtime.settings.auth.enabled:
        raise HTTPException(status_code=400, detail="鉴权未启用，无需修改密码")
    stored = await runtime.db.user_repo.get_password_hash(current.id)
    if not stored or not verify_password(body.old_password or "", stored):
        raise HTTPException(status_code=400, detail="原密码不正确")
    min_len = runtime.settings.auth.password_min_length
    if len(body.new_password) < min_len:
        raise HTTPException(status_code=400, detail=f"新密码长度至少 {min_len} 位")
    await runtime.db.user_repo.set_password(current.id, hash_password(body.new_password))
    return {"ok": True}
