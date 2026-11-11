"""RBAC 守卫口径一致性（require_permission / export_gate / 前端 hasPerm 三处同源）。

背景（本次修复的 bug）：端点守卫 `require_permission` 原先走
`roles.role_has_any_permission(current.role, ...)`，只读 `roles._ROLES` **静态字典**；
而 `get_current_user.permissions` 是 **DB 优先**（`role_permissions_from` 读 roles 表）。
两者口径不一的后果：管理面新建的自定义角色——权限表里明明勾了——永远过不了端点守卫，
实测报 `403 权限不足：需要 obs`。前端 `hasPerm` 读的是 `user.permissions`，于是还会出现
"按钮显示了、点了却 403" 的不一致。

修复后三者统一为：`permissions_grant(effective_permissions(role, permissions), wanted)`
——身份携带的 permissions 优先、缺失时回落静态角色定义。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from dmf_agent.auth.dependencies import require_permission
from dmf_agent.auth.roles import (
    PERM_EXPORT_APPROVE,
    PERM_OBS,
    effective_permissions,
    permissions_grant,
)
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.security.export_gate import assert_export_allowed


def _user(role: str, permissions=None) -> CurrentUser:
    return CurrentUser(
        id="u1",
        username="u1",
        display_name="u1",
        tenant_id="t1",
        tenant_code="T1",
        role=role,
        permissions=permissions,
    )


async def _call(perms: str | tuple[str, ...], user: CurrentUser) -> CurrentUser:
    """直接调用守卫依赖（不走 HTTP，聚焦判定逻辑）。"""
    wanted = (perms,) if isinstance(perms, str) else tuple(perms)
    return await require_permission(*wanted)(user)


# ---------- 核心回归：自定义角色（不在 _ROLES 静态表里） ----------


async def test_custom_role_with_permission_passes_guard():
    """回归点：自定义角色带权限必须放行（旧逻辑只读静态表 → 必然 403）。"""
    user = _user("exporter", ["obs", "fewshot"])
    assert (await _call(PERM_OBS, user)).role == "exporter"


async def test_custom_role_without_permission_rejected():
    """自定义角色但没勾该权限 → 仍应 403（不能因为"角色不认识"就放行）。"""
    with pytest.raises(HTTPException) as ei:
        await _call(PERM_OBS, _user("exporter", ["chat"]))
    assert ei.value.status_code == 403


async def test_custom_role_unknown_and_no_permissions_rejected():
    """完全陌生的角色 + 无权限 → 403（静态表里也没有，回落结果为空）。"""
    with pytest.raises(HTTPException) as ei:
        await _call(PERM_OBS, _user("ghost", []))
    assert ei.value.status_code == 403


# ---------- admin / 通配 ----------


async def test_admin_role_hard_passes_even_without_permissions():
    """admin 角色硬放行（既有契约，与 require_role 一致；不受权限表改动影响）。"""
    assert (await _call(PERM_OBS, _user("admin", []))).role == "admin"
    assert (await _call(PERM_OBS, _user("admin", []))).role == "admin"


async def test_wildcard_permission_passes_any():
    assert (await _call(PERM_OBS, _user("whatever", ["*"]))).role == "whatever"


# ---------- DB 优先：权限表收窄必须生效 ----------


async def test_db_narrowing_overrides_static_role():
    """DB 里把 analyst 收窄到只剩 chat → 请求 obs 应 403。

    这是"DB 优先"的关键：不能再按静态 analyst（含 obs）偷偷放行。
    """
    with pytest.raises(HTTPException) as ei:
        await _call(PERM_OBS, _user("analyst", ["chat"]))
    assert ei.value.status_code == 403
    assert "obs" in ei.value.detail


async def test_db_grant_beyond_static_role():
    """DB 里给 viewer 额外授 obs（静态 viewer 没有）→ 应放行。"""
    assert (await _call(PERM_OBS, _user("viewer", ["chat", "obs"]))).role == "viewer"


# ---------- 回落：permissions 缺失时按静态角色定义 ----------


async def test_empty_permissions_falls_back_to_static_role():
    """permissions 缺失/为空时回落静态定义，避免"权限凭空消失"。"""
    assert (await _call(PERM_OBS, _user("analyst", []))).role == "analyst"  # 静态 analyst 有 obs
    with pytest.raises(HTTPException):
        await _call(PERM_OBS, _user("viewer", []))  # 静态 viewer 无 obs


# ---------- 多候选权限点（any 语义） ----------


async def test_any_of_multiple_permissions_passes():
    user = _user("exporter", ["slang"])
    assert (await _call(("obs", "slang"), user)).role == "exporter"


# ---------- 与导出闸门同源 ----------


async def test_export_gate_shares_the_same_rule():
    """export_gate 不得再长一份私有的"* 或 role==admin"判据。"""
    runtime = _runtime(require_approval=True)

    # 自定义角色带 export:approve → 放行
    assert_export_allowed(runtime, _user("exporter", [PERM_EXPORT_APPROVE]), kind="obs")

    # 自定义角色不带 → 403
    with pytest.raises(HTTPException) as ei:
        assert_export_allowed(runtime, _user("exporter", ["obs"]), kind="obs")
    assert ei.value.status_code == 403
    assert PERM_EXPORT_APPROVE in ei.value.detail

    # admin 硬放行（permissions 为空也能过）
    assert_export_allowed(runtime, _user("admin", []), kind="obs")

    # 闸门关闭 → 不校验
    assert_export_allowed(_runtime(require_approval=False), _user("viewer", []), kind="obs")


def _runtime(require_approval: bool):
    from types import SimpleNamespace

    return SimpleNamespace(
        settings=SimpleNamespace(
            security=SimpleNamespace(
                export_require_approval=require_approval,
                export_max_rows=5000,
                export_watermark_enabled=True,
            )
        )
    )


# ---------- 纯函数（口径的单一实现） ----------


def test_permissions_grant_unit():
    assert permissions_grant(["*"], ["obs"]) is True
    assert permissions_grant(["obs"], ["obs"]) is True
    assert permissions_grant(["obs"], ["chat"]) is False
    assert permissions_grant([], ["obs"]) is False
    assert permissions_grant(None, ["obs"]) is False
    assert permissions_grant(["chat"], ["obs", "chat"]) is True  # any 语义
    # wanted 为空：只有通配才算命中（用于 "是否 admin 级别" 的判定）
    assert permissions_grant(["*"], []) is True
    assert permissions_grant(["obs"], []) is False


def test_effective_permissions_unit():
    # 有就用传入的（DB 优先）
    assert effective_permissions("viewer", ["obs"]) == ["obs"]
    # 空列表视为"缺权限信息" → 回落静态
    assert effective_permissions("analyst", []) != []
    assert "obs" in effective_permissions("analyst", None)
    # 未知角色回落为空
    assert effective_permissions("ghost", None) == []
