"""认证 / 租户 / 用户 / 角色测试（独立 app，鉴权开启；不依赖外部服务）。"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from dmf_agent.api.routers import (
    auth as auth_router,
    knowledge as knowledge_router,
    memory as memory_router,
    obs as obs_router,
    plugins as plugins_router,
    prompts as prompts_router,
    roles as roles_router,
    semantic as semantic_router,
    sessions as sessions_router,
    tenants as tenants_router,
    users as users_router,
)
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.auth.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from dmf_agent.auth.service import AuthError, AuthService
from dmf_agent.config.settings import Settings
from dmf_agent.db.factory import build_database
from dmf_agent.schemas.knowledge import DocumentData
from dmf_agent.services.plugins import PluginRegistry
from dmf_agent.services.prompts import PromptManager
from dmf_agent.services.runtime import AppRuntime

ADMIN_PW = "admin123"


def _settings() -> Settings:
    s = Settings()
    s.db.enabled = False
    s.auth.enabled = True
    s.auth.jwt_secret = "unit-test-secret-key-at-least-32-bytes!!"
    return s


def _build_client() -> tuple[TestClient, object]:
    settings = _settings()
    db = build_database(settings)
    asyncio.run(AuthService(db, settings).ensure_default_admin())
    app = FastAPI()
    auth_dep = [Depends(get_current_user)]
    app.include_router(auth_router.router, prefix="/api/v1")
    app.include_router(roles_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(sessions_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(tenants_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(users_router.router, prefix="/api/v1", dependencies=auth_dep)
    # 细粒度权限点 / 文档级租户隔离：一起挂进来（守卫在路由级，无需装配真实引擎）
    app.include_router(knowledge_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(memory_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(semantic_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(obs_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(plugins_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.include_router(prompts_router.router, prefix="/api/v1", dependencies=auth_dep)
    app.state.runtime = AppRuntime(
        settings=settings,
        db=db,
        graph=None,
        chat_service=None,
        retriever=None,
        rag_backend="bm25",
        # 管理端服务用内存存储（插件列表 / 提示词版本）——测试只验权限与基本读写
        plugin_registry=PluginRegistry(db.plugin_store),
        prompt_manager=PromptManager(db.prompt_store),
    )
    return TestClient(app), db


def _login(client: TestClient, username: str = "admin", password: str = ADMIN_PW) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------- 口令哈希 / 令牌 ----------


def test_password_hash_roundtrip():
    h = hash_password("s3cret!")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret!", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("x", "not-a-valid-hash") is False


def test_token_roundtrip_and_tamper():
    token, ttl = create_access_token(
        user_id="u1", username="alice", tenant_id="t1", tenant_code="default",
        role="analyst", secret="k" * 32, ttl_minutes=10,
    )
    assert ttl == 600
    payload = decode_token(token, "k" * 32)
    assert payload["sub"] == "u1" and payload["role"] == "analyst" and payload["tenant_code"] == "default"
    # 错误密钥 / 篡改令牌 -> 解析失败
    for bad_secret, bad_token in (("x" * 32, token), ("k" * 32, token + "x")):
        try:
            decode_token(bad_token, bad_secret)
            raise AssertionError("应当解析失败")
        except Exception:
            pass


# ---------- 登录服务 ----------


def test_auth_service_login_success_and_failure():
    settings = _settings()
    db = build_database(settings)
    svc = AuthService(db, settings)

    async def go():
        await svc.ensure_default_admin()
        tr = await svc.login("admin", ADMIN_PW)
        assert tr.user.username == "admin" and tr.user.role == "admin"
        assert tr.user.tenant_code == "default" and tr.access_token
        for bad in ("nope", "Admin", ""):
            try:
                await svc.login("admin", bad)
                raise AssertionError("错误口令应被拒绝")
            except AuthError:
                pass
        try:
            await svc.login("ghost", ADMIN_PW)
            raise AssertionError("不存在用户应被拒绝")
        except AuthError:
            pass

    asyncio.run(go())


# ---------- HTTP：登录与鉴权 ----------


def test_http_requires_token_then_login():
    client, _db = _build_client()
    # 无令牌 -> 401
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.get("/api/v1/tenants").status_code == 401
    # 非法令牌 -> 401
    assert client.get("/api/v1/sessions", headers={"Authorization": "Bearer bad"}).status_code == 401
    # 错口令 -> 401
    assert client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "x"}
    ).status_code == 401
    # 登录 -> me
    headers = _login(client)
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["username"] == "admin"


def test_roles_endpoint_and_admin_crud():
    client, _db = _build_client()
    H = _login(client)

    roles = client.get("/api/v1/roles", headers=H).json()
    assert {r["name"] for r in roles} == {"admin", "analyst", "viewer"}

    # 建租户 + 建用户
    t = client.post("/api/v1/tenants", json={"code": "acme", "name": "ACME"}, headers=H)
    assert t.status_code == 200, t.text
    tenant_id = t.json()["id"]
    # 重复 code -> 409
    assert client.post("/api/v1/tenants", json={"code": "acme", "name": "dup"}, headers=H).status_code == 409

    u = client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "bob12345", "tenant_id": tenant_id, "role": "analyst"},
        headers=H,
    )
    assert u.status_code == 200, u.text
    assert u.json()["tenant_code"] == "acme"
    uid = u.json()["id"]
    # 用户名重复 -> 409；密码过短 -> 400
    assert client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "bob12345", "tenant_id": tenant_id, "role": "viewer"},
        headers=H,
    ).status_code == 409
    assert client.post(
        "/api/v1/users",
        json={"username": "short", "password": "1", "tenant_id": tenant_id, "role": "viewer"},
        headers=H,
    ).status_code == 400

    # 列表 + 改密 + 停用
    assert len(client.get("/api/v1/users", headers=H).json()) == 2
    assert client.post(f"/api/v1/users/{uid}/password", json={"new_password": "newpass123"}, headers=H).status_code == 200
    assert client.patch(f"/api/v1/users/{uid}", json={"status": "disabled"}, headers=H).json()["status"] == "disabled"
    # 停用后无法登录
    assert client.post("/api/v1/auth/login", json={"username": "bob", "password": "newpass123"}).status_code == 401


def test_role_guard_analyst_cannot_manage_tenants():
    client, _db = _build_client()
    H = _login(client)
    tenant_id = client.post("/api/v1/tenants", json={"code": "t2", "name": "T2"}, headers=H).json()["id"]
    client.post(
        "/api/v1/users",
        json={"username": "carol", "password": "carol123", "tenant_id": tenant_id, "role": "analyst"},
        headers=H,
    )
    HA = _login(client, "carol", "carol123")
    # analyst 不能管理租户 / 用户
    assert client.get("/api/v1/tenants", headers=HA).status_code == 403
    assert client.get("/api/v1/users", headers=HA).status_code == 403
    # 但可访问业务接口
    assert client.get("/api/v1/sessions", headers=HA).status_code == 200
    # 角色管理（含只读列表）已收敛为 admin 管理面能力（2026-09-16 角色 DB 化）
    assert client.get("/api/v1/roles", headers=HA).status_code == 403


def test_session_tenant_isolation():
    client, _db = _build_client()
    H = _login(client)  # admin，default 租户

    # default 租户建一个会话
    r = client.post("/api/v1/sessions", json={"title": "secret"}, headers=H)
    assert r.status_code == 200
    default_sid = r.json()["id"]
    assert len(client.get("/api/v1/sessions", headers=H).json()) == 1

    # 另一个租户的用户不应看到 default 的会话
    tenant_id = client.post("/api/v1/tenants", json={"code": "other", "name": "Other"}, headers=H).json()["id"]
    client.post(
        "/api/v1/users",
        json={"username": "dave", "password": "dave1234", "tenant_id": tenant_id, "role": "viewer"},
        headers=H,
    )
    HD = _login(client, "dave", "dave1234")
    assert client.get("/api/v1/sessions", headers=HD).json() == []
    # 跨租户按会话 id 直取 -> 404（不泄露存在性）
    assert client.get(f"/api/v1/sessions/{default_sid}", headers=HD).status_code == 404
    assert client.delete(f"/api/v1/sessions/{default_sid}", headers=HD).status_code == 404

    # 删除有用户的租户 -> 409
    assert client.delete(f"/api/v1/tenants/{tenant_id}", headers=H).status_code == 409


# ---------- 刷新令牌（access 到期静默续期）----------


def test_refresh_token_flow():
    client, _db = _build_client()
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    body = r.json()
    refresh = body["refresh_token"]
    assert refresh and body["refresh_expires_in"] > body["expires_in"]

    # 用 refresh 换新的 access + 新的 refresh（滚动轮换）
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 200, r2.text
    new_body = r2.json()
    assert new_body["access_token"] and new_body["refresh_token"]
    assert client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_body['access_token']}"}
    ).status_code == 200

    # 拿 refresh 令牌当 access 用 -> 401（typ 声明校验）
    assert client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"}
    ).status_code == 401
    # 伪造 / 空刷新令牌 -> 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": "bad.token.x"}).status_code == 401


def test_refresh_rejected_for_disabled_user():
    client, _db = _build_client()
    H = _login(client)
    tenant_id = client.post("/api/v1/tenants", json={"code": "rz", "name": "RZ"}, headers=H).json()["id"]
    client.post(
        "/api/v1/users",
        json={"username": "erin", "password": "erin1234", "tenant_id": tenant_id, "role": "viewer"},
        headers=H,
    )
    li = client.post("/api/v1/auth/login", json={"username": "erin", "password": "erin1234"}).json()
    # 停用后，旧刷新令牌不再可用（续期时重新读库，角色/状态即时生效）
    uid = [u for u in client.get("/api/v1/users", headers=H).json() if u["username"] == "erin"][0]["id"]
    client.patch(f"/api/v1/users/{uid}", json={"status": "disabled"}, headers=H)
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": li["refresh_token"]}
    ).status_code == 401


# ---------- 接口级权限点（viewer / analyst 差异）----------


def _make_user(client, H, code: str, username: str, role: str) -> str:
    tenant_id = client.post("/api/v1/tenants", json={"code": code, "name": code.upper()}, headers=H).json()["id"]
    client.post(
        "/api/v1/users",
        json={"username": username, "password": "pw123456", "tenant_id": tenant_id, "role": role},
        headers=H,
    )
    return tenant_id


def test_permission_guards_analyst_vs_viewer():
    client, _db = _build_client()
    H = _login(client)
    _make_user(client, H, "an", "anna", "analyst")
    _make_user(client, H, "vw", "vic", "viewer")
    HA = _login(client, "anna", "pw123456")
    HV = _login(client, "vic", "pw123456")

    # 只读语义元信息：两种角色都可（引擎未装配 -> 503，但绝不是 403）
    for Hx in (HA, HV):
        assert client.get("/api/v1/semantic/metrics", headers=Hx).status_code in (200, 503)
        assert client.post("/api/v1/semantic/compile", json={"metrics": ["GMV"]}, headers=Hx).status_code in (200, 503)
    # 执行语义查询：viewer 被拒（403），analyst 放行（未装配引擎 -> 503）
    assert client.post("/api/v1/semantic/query", json={"metrics": ["GMV"]}, headers=HV).status_code == 403
    assert client.post("/api/v1/semantic/query", json={"metrics": ["GMV"]}, headers=HA).status_code in (200, 503)

    # 知识库：读两种角色都可；写仅 analyst
    assert client.get("/api/v1/knowledge/documents", headers=HV).status_code == 200
    assert client.post(
        "/api/v1/knowledge/bases", json={"name": "vw-kb", "type": "personal"}, headers=HV
    ).status_code == 403
    assert client.post(
        "/api/v1/knowledge/bases", json={"name": "an-kb", "type": "personal"}, headers=HA
    ).status_code == 200

    # 平台配置：插件 / 提示词 / 观测 / L3 沉淀 —— viewer 一律 403
    assert client.get("/api/v1/plugins/skills", headers=HV).status_code == 403
    assert client.get("/api/v1/prompts/list", headers=HV).status_code == 403
    assert client.get("/api/v1/obs/summary", headers=HV).status_code == 403
    assert client.get("/api/v1/memory/l3", headers=HV).status_code == 403
    # analyst 可看观测 / 提示词 / 插件，但插件与提示词不可改
    assert client.get("/api/v1/plugins/skills", headers=HA).status_code == 200
    assert client.get("/api/v1/prompts/list", headers=HA).status_code == 200
    assert client.post(
        "/api/v1/plugins/skills", json={"name": "s", "description": ""}, headers=HA
    ).status_code == 403

    # 自己的偏好（L2）任何角色都可管理
    assert client.get("/api/v1/memory/preferences", headers=HV).status_code == 200


# ---------- 文档级租户隔离 ----------


def test_document_tenant_isolation():
    client, db = _build_client()
    H = _login(client)  # admin，default 租户

    t_other = _make_user(client, H, "doc", "dora", "analyst")
    tenant_default = client.get("/api/v1/auth/me", headers=H).json()["tenant_id"]

    async def seed():
        # 两个租户各一条**未归类**文档（老实现里这种文档不带租户 -> 跨租户可见）
        await db.document_repo.add(
            DocumentData(id="", title="default-doc", tenant_id=tenant_default)
        )
        await db.document_repo.add(
            DocumentData(id="", title="other-doc", tenant_id=t_other)
        )

    asyncio.run(seed())

    titles = {d["title"] for d in client.get("/api/v1/knowledge/documents", headers=H).json()}
    assert titles == {"default-doc"}

    HD = _login(client, "dora", "pw123456")
    titles_other = {d["title"] for d in client.get("/api/v1/knowledge/documents", headers=HD).json()}
    assert titles_other == {"other-doc"}

    # 跨租户按 id 删除 -> 404（不泄露存在性）
    default_doc_id = client.get("/api/v1/knowledge/documents", headers=H).json()[0]["id"]
    assert client.delete(f"/api/v1/knowledge/documents/{default_doc_id}", headers=HD).status_code == 404
    # 本租户删除 -> 200
    assert client.delete(f"/api/v1/knowledge/documents/{default_doc_id}", headers=H).status_code == 200
