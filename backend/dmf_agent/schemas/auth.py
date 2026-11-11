"""认证 / 租户 / 用户 / 角色 相关 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------- 租户 ----------


class TenantData(BaseModel):
    id: str
    code: str
    name: str
    status: str = "active"  # active | disabled
    created_at: Optional[datetime] = None


class TenantCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="租户编码（唯一）")
    name: str = Field(..., min_length=1, max_length=128, description="租户名称")


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")


# ---------- 用户 ----------


class UserData(BaseModel):
    """用户对外视图（不含口令哈希）。"""

    id: str
    username: str
    display_name: str = ""
    tenant_id: str
    tenant_code: str = ""  # 冗余展示用（由路由层补全）
    role: str = "viewer"
    status: str = "active"  # active | disabled
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


# 角色标识格式（内置 ∪ 自定义角色均按此格式；存在性由 users 路由查 roles 表校验）
_ROLE_NAME_PATTERN = r"^[a-z0-9_]{2,32}$"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)
    display_name: str = ""
    tenant_id: str = Field(..., description="所属租户 id")
    role: str = Field("viewer", pattern=_ROLE_NAME_PATTERN)


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    tenant_id: Optional[str] = None
    role: Optional[str] = Field(None, pattern=_ROLE_NAME_PATTERN)
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")


class PasswordChange(BaseModel):
    """改密：用户自助需提供 old_password；管理员重置不校验旧密码（由路由控制）。"""

    old_password: Optional[str] = None
    new_password: str = Field(..., min_length=1)


# ---------- 登录 / 令牌 ----------


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class RefreshRequest(BaseModel):
    """刷新令牌换新访问令牌；refresh_token 由登录 / 上次刷新返回。"""

    refresh_token: str = Field(..., min_length=1)


class CurrentUser(BaseModel):
    """当前登录用户（令牌解出，注入路由）。

    permissions 为该角色对应的权限点集合（admin 为 `["*"]`），
    由 roles.role_permissions(role) 计算，供前端显隐入口与后端 require_permission 判定。
    """

    id: str
    username: str
    display_name: str = ""
    tenant_id: str
    tenant_code: str = ""
    role: str = "viewer"
    permissions: List[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 访问令牌剩余秒数
    # 刷新令牌（可换取新访问令牌；仅在 /auth/login、/auth/refresh 返回）
    refresh_token: str = ""
    refresh_expires_in: int = 0
    user: CurrentUser


# ---------- 角色 ----------


class RoleInfo(BaseModel):
    name: str
    label: str
    description: str
    permissions: List[str]
    status: str = "active"
    is_builtin: bool = False
    created_at: Optional[datetime] = None


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=32, description="角色标识 [a-z0-9_]")
    label: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    permissions: List[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=64)
    description: Optional[str] = None
    permissions: Optional[List[str]] = None
    status: Optional[str] = None


class RoleData(BaseModel):
    """角色持久化载体（仓储层）。"""

    name: str
    label: str = ""
    description: str = ""
    permissions: List[str] = Field(default_factory=list)
    status: str = "active"
    is_builtin: bool = False
    created_at: Optional[datetime] = None
