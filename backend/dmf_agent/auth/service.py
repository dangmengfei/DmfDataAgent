"""认证服务：登录签发令牌 / 刷新令牌换新 / 首次启动引导默认租户 + 管理员。"""

from __future__ import annotations

import logging
from typing import Optional

from dmf_agent.auth.roles import role_permissions_from
from dmf_agent.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from dmf_agent.config.settings import Settings
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.auth import CurrentUser, TenantData, TokenResponse, UserData

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """认证失败（凭据错误 / 账号停用 / 刷新令牌无效）。"""


class AuthService:
    def __init__(self, db: DatabaseBundle, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    # ---------- 令牌发放 ----------

    async def _issue_tokens(self, user: UserData) -> TokenResponse:
        """按用户当前库中状态签发 access + refresh（角色 / 租户以库为准）。

        刷新时走同一路径，因此"改角色 / 停用账号 / 换租户 / 调整角色权限"在下次续期即生效，
        无需等访问令牌自然过期。角色权限优先读 roles 表（含自定义角色），DB 异常回落静态定义。
        """
        tenant = await self._db.tenant_repo.get(user.tenant_id)
        tenant_code = tenant.code if tenant else ""
        permissions = await role_permissions_from(self._db.role_repo, user.role)
        if permissions is None:
            # 角色被显式禁用：与账号停用同语义，拒发令牌
            raise AuthError("用户角色已被停用，请联系管理员")
        auth = self._settings.auth
        access_token, expires_in = create_access_token(
            user_id=user.id,
            username=user.username,
            tenant_id=user.tenant_id,
            tenant_code=tenant_code,
            role=user.role,
            secret=auth.jwt_secret,
            algorithm=auth.jwt_algorithm,
            ttl_minutes=auth.access_token_ttl_minutes,
        )
        refresh_token, refresh_expires_in = create_refresh_token(
            user_id=user.id,
            username=user.username,
            secret=auth.jwt_secret,
            algorithm=auth.jwt_algorithm,
            ttl_days=auth.refresh_token_ttl_days,
        )
        return TokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=refresh_token,
            refresh_expires_in=refresh_expires_in,
            user=CurrentUser(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                tenant_id=user.tenant_id,
                tenant_code=tenant_code,
                role=user.role,
                permissions=permissions,
            ),
        )

    async def login(self, username: str, password: str) -> TokenResponse:
        user = await self._db.user_repo.get_by_username(username)
        # 统一错误信息，避免暴露"用户是否存在"
        if user is None or user.status != "active":
            raise AuthError("用户名或密码错误")
        stored = await self._db.user_repo.get_password_hash(user.id)
        if not stored or not verify_password(password, stored):
            raise AuthError("用户名或密码错误")

        await self._db.user_repo.touch_login(user.id)
        return await self._issue_tokens(user)

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """用刷新令牌换发新的访问令牌（同时轮换刷新令牌）。

        校验链：签名/过期 -> typ=refresh -> 用户存在且 active -> 租户可用。
        任一环节失败统一抛 AuthError（路由转 401），提示重新登录。
        """
        auth = self._settings.auth
        try:
            payload = decode_token(
                refresh_token, auth.jwt_secret, auth.jwt_algorithm, expected_type="refresh"
            )
        except Exception as exc:  # noqa: BLE001 - 无效/过期/类型不符统一按认证失败处理
            logger.info("刷新令牌校验失败：%s", exc)
            raise AuthError("刷新令牌无效或已过期，请重新登录")

        user_id = str(payload.get("sub") or "")
        user = await self._db.user_repo.get(user_id) if user_id else None
        if user is None or user.status != "active":
            raise AuthError("账号不存在或已停用，请重新登录")

        tenant = await self._db.tenant_repo.get(user.tenant_id)
        if tenant is not None and tenant.status != "active":
            raise AuthError("所属租户已停用，请联系管理员")

        return await self._issue_tokens(user)

    # ---------- 启动引导 ----------

    async def ensure_default_admin(self) -> Optional[UserData]:
        """无任何用户时引导默认租户 + 管理员（返回创建的管理员；已有用户返回 None）。"""
        if await self._db.user_repo.count() > 0:
            return None
        auth = self._settings.auth
        tenant = await self._db.tenant_repo.get_by_code(auth.default_tenant_code)
        if tenant is None:
            tenant = await self._db.tenant_repo.add(
                TenantData(id="", code=auth.default_tenant_code, name=auth.default_tenant_name)
            )
        admin = await self._db.user_repo.add(
            UserData(
                id="",
                username=auth.default_admin_username,
                display_name="管理员",
                tenant_id=tenant.id,
                role="admin",
            ),
            hash_password(auth.default_admin_password),
        )
        logger.warning(
            "已引导默认管理员：username=%s password=%s（请登录后尽快修改）",
            auth.default_admin_username,
            auth.default_admin_password,
        )
        return admin
