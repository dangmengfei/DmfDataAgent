"""角色管理（RBAC 角色 DB 化）测试。

覆盖：
- 内置角色种子幂等导入 / roles 列表（DB 空回落静态）
- 自定义角色增删改查（name 合法性 / 权限点校验 / 通配符拒绝）
- 内置角色保护（不可删 / 权限不可改 / admin 不可停用）
- 删除引用检查（有用户使用则 409）
- 登录与令牌鉴权展开自定义角色权限；禁用角色拒登录 / 401
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from dmf_agent.api.routers import (
    auth as auth_router,
    roles as roles_router,
    tenants as tenants_router,
    users as users_router,
)
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.auth.roles import seed_builtin_roles
from dmf_agent.auth.service import AuthError, AuthService
from dmf_agent.config.settings import Settings
from dmf_agent.db.factory import build_database
from dmf_agent.schemas.auth import RoleData
from dmf_agent.services.runtime import AppRuntime


def _settings() -> Settings:
    s = Settings()
    s.db.enabled = False
    s.auth.enabled = True
    s.auth.jwt_secret = "role-unit-test-secret-key-32-bytes!!!"
    return s


def _build() -> tuple[TestClient, AppRuntime]:
    settings = _settings()
    db = build_database(settings)
    asyncio.run(AuthService(db, settings).ensure_default_admin())
    app = FastAPI()
    dep = [Depends(get_current_user)]
    app.include_router(auth_router.router, prefix="/api/v1")
    app.include_router(roles_router.router, prefix="/api/v1", dependencies=dep)
    app.include_router(tenants_router.router, prefix="/api/v1", dependencies=dep)
    app.include_router(users_router.router, prefix="/api/v1", dependencies=dep)
    app.state.runtime = AppRuntime(
        settings=settings, db=db, graph=None, chat_service=None,
        retriever=None, rag_backend="bm25",
    )
    return TestClient(app), db


def _login(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------- 种子 ----------


async def test_seed_builtin_roles_idempotent():
    db = build_database(_settings())
    assert await seed_builtin_roles(db.role_repo) == 3
    assert await seed_builtin_roles(db.role_repo) == 0  # 幂等
    names = {r.name for r in await db.role_repo.list_all()}
    assert names == {"admin", "analyst", "viewer"}
    admin = await db.role_repo.get("admin")
    assert admin.is_builtin and admin.permissions == ["*"]


def test_roles_list_falls_back_to_static_when_empty():
    client, _ = _build()  # 未跑种子 -> roles 表空 -> 静态三级
    H = _login(client, "admin", "admin123")
    roles = client.get("/api/v1/roles", headers=H).json()
    assert {r["name"] for r in roles} == {"admin", "analyst", "viewer"}


# ---------- 自定义角色 CRUD ----------


def test_custom_role_crud_full_flow():
    client, db = _build()
    asyncio.run(seed_builtin_roles(db.role_repo))
    H = _login(client, "admin", "admin123")

    # 新增
    r = client.post("/api/v1/roles", headers=H, json={
        "name": "data_steward", "label": "数据专员",
        "description": "知识库与样本维护", "permissions": ["chat", "knowledge", "fewshot"],
    })
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["label"] == "数据专员" and created["permissions"] == ["chat", "knowledge", "fewshot"]
    assert created["is_builtin"] is False

    # 列表含自定义角色
    names = {x["name"] for x in client.get("/api/v1/roles", headers=H).json()}
    assert "data_steward" in names

    # 权限点清单
    perms = client.get("/api/v1/roles/permissions", headers=H).json()
    assert "chat" in perms and "*" not in perms

    # 改权限
    r = client.put("/api/v1/roles/data_steward", headers=H, json={
        "label": "数据专员+", "permissions": ["chat", "knowledge", "fewshot", "memory"],
    })
    assert r.status_code == 200
    assert r.json()["permissions"] == ["chat", "knowledge", "fewshot", "memory"]

    # 校验拒绝：未知权限点 / 通配符 / 非法名（1 位）/ 重名
    assert client.post("/api/v1/roles", headers=H, json={
        "name": "xx", "label": "X", "permissions": ["no_such_perm"]}).status_code == 400
    assert client.post("/api/v1/roles", headers=H, json={
        "name": "wild", "label": "X", "permissions": ["*"]}).status_code == 400
    assert client.post("/api/v1/roles", headers=H, json={
        "name": "b", "label": "X", "permissions": []}).status_code in (400, 422)  # 长度不合法
    assert client.post("/api/v1/roles", headers=H, json={
        "name": "analyst", "label": "X", "permissions": []}).status_code == 409

    # 用自定义角色建用户 -> 登录展开权限正确
    tenants = client.get("/api/v1/tenants", headers=H).json()
    r = client.post("/api/v1/users", headers=H, json={
        "username": "steward1", "password": "abc123", "display_name": "专员",
        "tenant_id": tenants[0]["id"], "role": "data_steward",
    })
    assert r.status_code == 200, r.text
    login = client.post("/api/v1/auth/login", json={"username": "steward1", "password": "abc123"})
    user = login.json()["user"]
    assert user["role"] == "data_steward"
    assert set(user["permissions"]) == {"chat", "knowledge", "fewshot", "memory"}

    # 有引用 -> 删除 409
    r = client.delete("/api/v1/roles/data_steward", headers=H)
    assert r.status_code == 409 and "steward1" in r.json()["detail"]

    # 迁移用户后可删
    users = client.get("/api/v1/users", headers=H).json()
    steward = next(u for u in users if u["username"] == "steward1")
    client.patch(f"/api/v1/users/{steward['id']}", headers=H, json={"role": "viewer"})
    assert client.delete("/api/v1/roles/data_steward", headers=H).status_code == 200
    assert client.delete("/api/v1/roles/data_steward", headers=H).status_code == 404


# ---------- 内置角色保护 ----------


def test_builtin_role_protection():
    client, db = _build()
    asyncio.run(seed_builtin_roles(db.role_repo))
    H = _login(client, "admin", "admin123")

    # 内置不可删
    assert client.delete("/api/v1/roles/admin", headers=H).status_code == 409
    assert client.delete("/api/v1/roles/viewer", headers=H).status_code == 409
    # 内置权限不可改
    r = client.put("/api/v1/roles/viewer", headers=H, json={"permissions": ["chat", "obs"]})
    assert r.status_code == 409
    # admin 不可停用（其余内置可停）
    assert client.put("/api/v1/roles/admin", headers=H, json={"status": "disabled"}).status_code == 409
    # 内置允许改 label
    r = client.put("/api/v1/roles/viewer", headers=H, json={"label": "只读用户改称"})
    assert r.status_code == 200 and r.json()["label"] == "只读用户改称"
    # 恢复（避免影响后续用例预期）
    client.put("/api/v1/roles/viewer", headers=H, json={"label": "只读用户"})


# ---------- 禁用角色登录拒绝 ----------


def test_disabled_role_blocks_login_and_access():
    client, db = _build()
    asyncio.run(seed_builtin_roles(db.role_repo))
    H = _login(client, "admin", "admin123")
    # 新增角色并给一个用户
    client.post("/api/v1/roles", headers=H, json={
        "name": "tmp_role", "label": "临时", "permissions": ["chat"]})
    tenants = client.get("/api/v1/tenants", headers=H).json()
    client.post("/api/v1/users", headers=H, json={
        "username": "tmpu", "password": "abc123", "display_name": "t",
        "tenant_id": tenants[0]["id"], "role": "tmp_role"})
    # 正常登录
    assert client.post("/api/v1/auth/login", json={"username": "tmpu", "password": "abc123"}).status_code == 200
    # 停用角色 -> 登录拒绝
    client.put("/api/v1/roles/tmp_role", headers=H, json={"status": "disabled"})
    r = client.post("/api/v1/auth/login", json={"username": "tmpu", "password": "abc123"})
    assert r.status_code == 401
    # 恢复后可登录
    client.put("/api/v1/roles/tmp_role", headers=H, json={"status": "active"})
    assert client.post("/api/v1/auth/login", json={"username": "tmpu", "password": "abc123"}).status_code == 200


async def test_role_permissions_from_fallbacks():
    from dmf_agent.auth.roles import role_permissions_from

    # repo 缺失 / 角色缺失 -> 静态
    assert await role_permissions_from(None, "viewer") == ["chat", "knowledge", "semantic", "feedback"]
    db = build_database(_settings())
    assert await role_permissions_from(db.role_repo, "viewer") == ["chat", "knowledge", "semantic", "feedback"]
    # 库中禁用 -> None
    await db.role_repo.add(RoleData(name="off", label="x", permissions=["chat"], status="disabled"))
    assert await role_permissions_from(db.role_repo, "off") is None
    # 库中自定义
    await db.role_repo.add(RoleData(name="cu", label="x", permissions=["chat", "obs"]))
    assert await role_permissions_from(db.role_repo, "cu") == ["chat", "obs"]


# ---------- Service 禁用角色拒发令牌 ----------


async def test_issue_tokens_rejects_disabled_role():
    settings = _settings()
    db = build_database(settings)
    await seed_builtin_roles(db.role_repo)
    service = AuthService(db, settings)
    # 停用 viewer
    await db.role_repo.update("viewer", status="disabled")
    from dmf_agent.schemas.auth import UserData

    user = UserData(id="u1", username="v1", tenant_id="", role="viewer")
    try:
        await service._issue_tokens(user)
        raise AssertionError("应拒发令牌")
    except AuthError as e:
        assert "停用" in str(e)
    # 恢复后可发
    await db.role_repo.update("viewer", status="active")
    resp = await service._issue_tokens(user)
    assert "access_token" in resp.model_dump()
