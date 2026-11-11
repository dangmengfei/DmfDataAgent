"""认证底层：口令哈希（PBKDF2-HMAC-SHA256）与 JWT 签发/校验。

- 口令不存明文：存储格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`，
  标准库实现，无额外依赖；校验用 hmac.compare_digest 抗时序攻击。
- JWT 使用 PyJWT（HS256），载荷含 sub/username/tenant_id/tenant_code/role/iat/exp/typ。
  `typ` 区分令牌用途：`access`（业务接口）与 `refresh`（仅用于续期，见 create_refresh_token）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import jwt

_ALGO = "pbkdf2_sha256"
# 迭代次数：兼顾安全与登录延迟（每次登录约 30~60ms）
_ITERATIONS = 120_000
_SALT_BYTES = 16

# 令牌用途（JWT `typ` 声明）
_TYPE_ACCESS = "access"
_TYPE_REFRESH = "refresh"


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令；格式非法或算法不符一律返回 False（不抛异常）。"""
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


def _create_token(
    *,
    user_id: str,
    username: str,
    tenant_id: str,
    tenant_code: str,
    role: str,
    secret: str,
    algorithm: str,
    ttl: timedelta,
    token_type: str,
) -> Tuple[str, int]:
    """签发 JWT（access / refresh 共用），返回 (token, expires_in_seconds)。"""
    now = datetime.now(timezone.utc)
    exp = now + ttl
    payload: Dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "tenant_id": tenant_id,
        "tenant_code": tenant_code,
        "role": role,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=algorithm)
    return token, int(ttl.total_seconds())


def create_access_token(
    *,
    user_id: str,
    username: str,
    tenant_id: str,
    tenant_code: str,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_minutes: int = 720,
) -> Tuple[str, int]:
    """签发访问令牌，返回 (token, expires_in_seconds)。"""
    return _create_token(
        user_id=user_id,
        username=username,
        tenant_id=tenant_id,
        tenant_code=tenant_code,
        role=role,
        secret=secret,
        algorithm=algorithm,
        ttl=timedelta(minutes=ttl_minutes),
        token_type=_TYPE_ACCESS,
    )


def create_refresh_token(
    *,
    user_id: str,
    username: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_days: int = 14,
) -> Tuple[str, int]:
    """签发刷新令牌，返回 (token, expires_in_seconds)。

    刷新令牌**不携带 role / tenant / 角色信息**：只能用于 `/auth/refresh` 换取新的
    访问令牌。换发时重新从库中读取用户与租户（角色变更、账号停用即时生效），
    因此刷新令牌被窃取的危害半径仅限于"能续期"，不能直接调用业务接口
    （`get_current_user` 只接受 access 令牌，见 `decode_token(expected_type=...)`）。
    """
    return _create_token(
        user_id=user_id,
        username=username,
        tenant_id="",
        tenant_code="",
        role="",
        secret=secret,
        algorithm=algorithm,
        ttl=timedelta(days=ttl_days),
        token_type=_TYPE_REFRESH,
    )


def decode_token(
    token: str,
    secret: str,
    algorithm: str = "HS256",
    expected_type: Optional[str] = None,
) -> Dict[str, Any]:
    """解析并校验令牌；无效/过期抛 jwt.PyJWTError。

    传 `expected_type` 时会额外校验 `typ` 声明：类型不符抛 InvalidTokenError，
    用于杜绝"拿 refresh 令牌当 access 令牌调用业务接口"。
    """
    payload = jwt.decode(token, secret, algorithms=[algorithm])
    if expected_type is not None and payload.get("typ", _TYPE_ACCESS) != expected_type:
        raise jwt.InvalidTokenError(f"令牌类型不符：期望 {expected_type}")
    return payload
