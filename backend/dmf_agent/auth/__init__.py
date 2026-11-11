"""认证与权限（JWT + 租户 / 角色）。"""

from dmf_agent.auth.dependencies import get_current_user, require_role
from dmf_agent.auth.roles import ADMIN, ALL_ROLES, ANALYST, VIEWER, can_write_knowledge, list_roles
from dmf_agent.auth.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from dmf_agent.auth.service import AuthError, AuthService

__all__ = [
    "AuthError",
    "AuthService",
    "ADMIN",
    "ANALYST",
    "VIEWER",
    "ALL_ROLES",
    "can_write_knowledge",
    "create_access_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "list_roles",
    "require_role",
    "verify_password",
]
