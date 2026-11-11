"""T198e：编辑重发「替换该轮」——按消息裁剪的 delete_after。

覆盖：
- 仓储三同步（InMemory）：inclusive / exclusive / 锚点不存在 / 空尾巴；
- 路由层：DELETE /sessions/{id}/messages/after/{message_id}
  （auth 关闭=合成 admin；跨会话锚点按 404 处理，避免越权裁剪别人会话）；
- 裁剪后结构化对话状态被重置（旧状态与历史不符，留着会污染下一轮上下文）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dmf_agent.api.deps import get_runtime
from dmf_agent.api.routers import sessions as sessions_router
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemoryMessageRepository
from dmf_agent.schemas.session import MessageData


async def _seed(repo: InMemoryMessageRepository, sid: str, n_rounds: int):
    """灌 n 轮（每轮 user+assistant），返回按顺序的消息 id 列表。"""
    ids = []
    for i in range(n_rounds):
        u = await repo.add(MessageData(id="", session_id=sid, role="user", content=f"q{i}"))
        a = await repo.add(MessageData(id="", session_id=sid, role="assistant", content=f"a{i}"))
        ids += [u.id, a.id]
    return ids


# ---------------- 仓储三同步 ----------------

@pytest.mark.asyncio
async def test_delete_after_inclusive_removes_anchor_and_tail():
    repo = InMemoryMessageRepository()
    ids = await _seed(repo, "s1", 3)
    # 从第 2 轮的 user 消息起裁掉 → 该条 + 其后 3 条
    anchor = ids[2]
    removed = await repo.delete_after("s1", anchor, inclusive=True)
    assert removed == 4
    left = await repo.list_by_session("s1")
    assert [m.id for m in left] == ids[:2]


@pytest.mark.asyncio
async def test_delete_after_exclusive_keeps_anchor():
    repo = InMemoryMessageRepository()
    ids = await _seed(repo, "s1", 2)
    # 保留锚点、只删其后（「重试最后一轮」语义）
    removed = await repo.delete_after("s1", ids[0], inclusive=False)
    assert removed == 3
    left = await repo.list_by_session("s1")
    assert [m.id for m in left] == [ids[0]]


@pytest.mark.asyncio
async def test_delete_after_unknown_anchor_is_noop():
    repo = InMemoryMessageRepository()
    ids = await _seed(repo, "s1", 2)
    assert await repo.delete_after("s1", "does-not-exist") == 0
    assert len(await repo.list_by_session("s1")) == len(ids)


@pytest.mark.asyncio
async def test_delete_after_last_message_removes_only_it():
    repo = InMemoryMessageRepository()
    ids = await _seed(repo, "s1", 2)
    removed = await repo.delete_after("s1", ids[-1], inclusive=True)
    assert removed == 1
    assert [m.id for m in await repo.list_by_session("s1")] == ids[:-1]


@pytest.mark.asyncio
async def test_delete_after_scoped_to_session():
    """锚点不属于该会话时必须不删（避免跨会话误裁）。"""
    repo = InMemoryMessageRepository()
    a_ids = await _seed(repo, "s1", 2)
    b_ids = await _seed(repo, "s2", 2)
    # 用 s2 的锚点去裁 s1 → 锚点不在 s1 的有序 id 里，返回 0
    assert await repo.delete_after("s1", b_ids[0]) == 0
    assert len(await repo.list_by_session("s1")) == len(a_ids)
    assert len(await repo.list_by_session("s2")) == len(b_ids)


# ---------------- 路由层 ----------------

async def _build_client_and_session():
    db = DatabaseBundle(mode="memory")
    session = await db.session_repo.create(user_id="u1", tenant_id="t1")
    rt = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=False)),
    )
    app = FastAPI()
    app.include_router(sessions_router.router)
    app.dependency_overrides[get_runtime] = lambda: rt
    return TestClient(app), session.id, db


@pytest.mark.asyncio
async def test_api_delete_after_truncates_and_resets_state():
    from dmf_agent.schemas.session import ConversationStateData

    client, sid, db = await _build_client_and_session()
    ids = await _seed(db.message_repo, sid, 3)
    # 先落一份对话状态，验证裁剪后会被重置
    await db.conversation_state_repo.upsert(
        ConversationStateData(session_id=sid, tenant_id="t1", user_id="u1", topic="存款")
    )

    r = client.delete(f"/sessions/{sid}/messages/after/{ids[2]}?inclusive=true")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 4
    assert body["state_reset"] is True
    left = await db.message_repo.list_by_session(sid)
    assert [m.id for m in left] == ids[:2]
    assert await db.conversation_state_repo.get(sid) is None


@pytest.mark.asyncio
async def test_api_delete_after_exclusive_param():
    client, sid, db = await _build_client_and_session()
    ids = await _seed(db.message_repo, sid, 2)
    r = client.delete(f"/sessions/{sid}/messages/after/{ids[0]}?inclusive=false")
    assert r.status_code == 200
    assert r.json()["deleted"] == 3
    assert [m.id for m in await db.message_repo.list_by_session(sid)] == [ids[0]]


@pytest.mark.asyncio
async def test_api_delete_after_unknown_message_404():
    client, sid, db = await _build_client_and_session()
    await _seed(db.message_repo, sid, 1)
    r = client.delete(f"/sessions/{sid}/messages/after/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_after_cross_session_message_404():
    """锚点属于别的会话 → 404（归属校验先于裁剪，防越权）。"""
    client, sid, db = await _build_client_and_session()
    other = await db.session_repo.create(user_id="u1", tenant_id="t1")
    await _seed(db.message_repo, sid, 1)
    o_ids = await _seed(db.message_repo, other.id, 1)
    r = client.delete(f"/sessions/{sid}/messages/after/{o_ids[0]}")
    assert r.status_code == 404
    # 两边都没被裁
    assert len(await db.message_repo.list_by_session(sid)) == 2
    assert len(await db.message_repo.list_by_session(other.id)) == 2


@pytest.mark.asyncio
async def test_delete_after_route_registered():
    paths = {r.path for r in sessions_router.router.routes}
    assert "/sessions/{session_id}/messages/after/{message_id}" in paths
