"""L2 用户偏好记忆测试（表 / 仓储 / 注入 / API / 点赞沉淀；mock 模式无外部依赖）。"""

from __future__ import annotations

import types as _types
from typing import Any, Dict, List

import pytest

from dmf_agent.agents.metric_agent.nodes.extract_elements import (
    _apply_preference_defaults,
    format_preferences,
)
from dmf_agent.api.routers import feedback as feedback_router
from dmf_agent.api.routers import memory as memory_router
from dmf_agent.config.settings import Settings
from dmf_agent.db.memory import (
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserPreferenceRepository,
)
from dmf_agent.memory.history import HistoryAssembler
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.memory import UserPreferenceData, UserPreferenceUpsert
from dmf_agent.services.chat_service import ChatService

# ---------- 仓储 ----------


def test_preference_repo_upsert_is_idempotent_per_key():
    repo = InMemoryUserPreferenceRepository()

    async def go():
        await repo.upsert(
            UserPreferenceData(id=0, user_id="u1", key="granularity", value="month")
        )
        await repo.upsert(
            UserPreferenceData(id=0, user_id="u1", key="granularity", value="week")
        )
        # 同一 (user_id, key) 只保留一条，后者覆盖前者
        items = await repo.list_by_user("u1")
        assert len(items) == 1 and items[0].value == "week"

        # 另一个用户互不影响
        await repo.upsert(UserPreferenceData(id=0, user_id="u2", key="granularity", value="day"))
        assert len(await repo.list_by_user("u1")) == 1
        assert len(await repo.list_by_user("u2")) == 1

        # 状态过滤
        await repo.upsert(
            UserPreferenceData(
                id=0, user_id="u1", key="old", value="x", status="archived"
            )
        )
        assert len(await repo.list_by_user("u1")) == 2
        assert len(await repo.list_by_user("u1", status="active")) == 1

        got = await repo.get("u1", "granularity")
        assert got is not None and got.value == "week"
        assert await repo.delete("u1", "granularity") is True
        assert await repo.get("u1", "granularity") is None
        assert await repo.delete("u1", "granularity") is False

        assert await repo.clear_user("u1") == 1
        assert await repo.list_by_user("u1") == []

    import asyncio

    asyncio.run(go())


# ---------- 渲染 / 默认值 ----------


def test_format_preferences_and_rule_defaults():
    state: Dict[str, Any] = {
        "preferences": [
            {"key": "granularity", "value": "month", "source": "user"},
            {"key": "dimensions", "value": "地区,渠道", "source": "inferred"},
        ]
    }
    text = format_preferences(state)
    assert "granularity：month" in text
    assert "地区,渠道" in text
    # 无偏好 -> 空串（不改动 prompt）
    assert format_preferences({}) == ""

    # 规则路径：只补空缺字段，不覆盖已解析值
    elements = {"time_range": {"start": "2026-08-01", "end": "2026-08-31"}, "dimensions": []}
    out = _apply_preference_defaults(elements, state)
    assert out["time_range"]["granularity"] == "month"
    assert out["dimensions"] == ["地区", "渠道"]

    # 已指定 -> 保留用户当场口径
    explicit = {"time_range": {"granularity": "day"}, "dimensions": ["分行"]}
    out2 = _apply_preference_defaults(explicit, state)
    assert out2["time_range"]["granularity"] == "day"
    assert out2["dimensions"] == ["分行"]


# ---------- 对话注入 ----------


class _CaptureGraph:
    """记录初始 state 的假图（ChatService 只消费 astream 的 (mode, payload) 二元组）。"""

    def __init__(self) -> None:
        self.states: List[Dict[str, Any]] = []

    async def astream(self, state, config, stream_mode=None):  # noqa: ANN001
        self.states.append(state)
        yield ("updates", {"synthesize_answer": {"final_answer": "ok"}})


def _chat_service(pref_repo, memory_disabled=None):
    settings = Settings()
    settings.db.enabled = False
    assembler = HistoryAssembler(
        message_repo=InMemoryMessageRepository(),
        summary_repo=_InMemSummary(),
        settings=settings,
        get_chat_model=lambda: None,
    )
    graph = _CaptureGraph()
    service = ChatService(
        graph=graph,
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
        history_assembler=assembler,
        preference_repo=pref_repo,
        memory_disabled=memory_disabled,
    )
    return service, graph


class _InMemSummary:
    """最小摘要仓储替身（HistoryAssembler 的 mock 路径不会真正写入）。"""

    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    async def get(self, session_id: str):
        return self._d.get(session_id)

    async def upsert(self, summary):
        self._d[summary.session_id] = summary
        return summary

    async def delete(self, session_id: str) -> None:
        self._d.pop(session_id, None)


async def _drain(agen):
    async for _ in agen:
        pass


async def test_chat_service_injects_active_preferences():
    repo = InMemoryUserPreferenceRepository()
    await repo.upsert(
        UserPreferenceData(id=0, user_id="u1", key="granularity", value="month")
    )
    await repo.upsert(
        UserPreferenceData(
            id=0, user_id="u1", key="archived_one", value="x", status="archived"
        )
    )
    service, graph = _chat_service(repo)
    await _drain(service.astream_chat(session_id=None, user_id="u1", message="查GMV", tenant_id="t1"))

    state = graph.states[0]
    assert state["tenant_id"] == "t1"
    # 只注入 active 偏好（archived 不进 prompt）
    assert [p["key"] for p in state["preferences"]] == ["granularity"]


async def test_memory_disabled_skips_preference_injection():
    repo = InMemoryUserPreferenceRepository()
    await repo.upsert(
        UserPreferenceData(id=0, user_id="u1", key="granularity", value="month")
    )
    service, graph = _chat_service(repo, memory_disabled={"u1": True})
    await _drain(service.astream_chat(session_id=None, user_id="u1", message="查GMV"))

    # 一键关闭记忆：L2 偏好与 L1 历史装配同步失效
    assert not graph.states[0].get("preferences")


# ---------- 点赞沉淀（inferred）----------


def _fake_db(session_tenant="t1", user_id="u1"):
    session = _types.SimpleNamespace(
        id="s1", user_id=user_id, tenant_id=session_tenant
    )
    return _types.SimpleNamespace(
        session_repo=_types.SimpleNamespace(get=_async_return(session)),
        preference_repo=InMemoryUserPreferenceRepository(),
    )


def _async_return(value):
    async def _inner(*_a, **_kw):
        return value

    return _inner


async def test_feedback_learns_inferred_preferences():
    db = _fake_db()
    msg = _types.SimpleNamespace(
        node_trace={
            "query_elements": {
                "time_range": {"granularity": "month"},
                "dimensions": ["地区"],
            }
        }
    )
    learned = await feedback_router._learn_preferences(db, "s1", msg)
    assert set(learned) == {"granularity=month", "dimensions=地区"}
    items = await db.preference_repo.list_by_user("u1")
    assert {p.key for p in items} == {"granularity", "dimensions"}
    assert all(p.source == "inferred" and p.confidence < 1.0 for p in items)

    # 重复点赞不再改写（值相同跳过）
    assert await feedback_router._learn_preferences(db, "s1", msg) == []

    # 用户显式设置过的偏好不被系统推断覆盖
    await db.preference_repo.upsert(
        UserPreferenceData(
            id=0, user_id="u1", key="granularity", value="week", source="user"
        )
    )
    msg2 = _types.SimpleNamespace(
        node_trace={"query_elements": {"time_range": {"granularity": "day"}}}
    )
    await feedback_router._learn_preferences(db, "s1", msg2)
    got = await db.preference_repo.get("u1", "granularity")
    assert got is not None and got.value == "week" and got.source == "user"


async def test_feedback_learn_skips_when_no_elements():
    db = _fake_db()
    assert await feedback_router._learn_preferences(db, "s1", _types.SimpleNamespace(node_trace={})) == []


# ---------- HTTP / 路由层 ----------


def _runtime():
    db = _types.SimpleNamespace(preference_repo=InMemoryUserPreferenceRepository())
    return _types.SimpleNamespace(db=db, memory_disabled={})


def _user(uid="u1", tenant="t1", role="analyst"):
    return CurrentUser(
        id=uid, username=uid, tenant_id=tenant, role=role, permissions=["*"]
    )


def test_preference_router_crud_and_identity_binding():
    import asyncio

    rt = _runtime()
    me = _user()

    async def go():
        created = await memory_router.upsert_preference(
            UserPreferenceUpsert(key="granularity", value="month"), current=me, runtime=rt
        )
        assert created.user_id == "u1" and created.tenant_id == "t1"

        items = await memory_router.list_preferences(status=None, user_id=None, current=me, runtime=rt)
        assert len(items) == 1 and items[0].value == "month"

        # 查他人偏好需要 memory 权限点：viewer 被拒
        viewer = _user(uid="u9", role="viewer")
        viewer.permissions = ["chat"]
        with pytest.raises(Exception) as exc:
            await memory_router.list_preferences(
                status=None, user_id="u1", current=viewer, runtime=rt
            )
        assert "403" in str(exc.value)

        # 删除 + 清空
        await memory_router.delete_preference("granularity", current=me, runtime=rt)
        assert await memory_router.list_preferences(status=None, user_id=None, current=me, runtime=rt) == []
        with pytest.raises(Exception):
            await memory_router.delete_preference("granularity", current=me, runtime=rt)

        await memory_router.upsert_preference(
            UserPreferenceUpsert(key="a", value="1"), current=me, runtime=rt
        )
        await memory_router.upsert_preference(
            UserPreferenceUpsert(key="b", value="2"), current=me, runtime=rt
        )
        assert (await memory_router.clear_preferences(current=me, runtime=rt))["removed"] == 2

    asyncio.run(go())


def test_memory_disable_ignores_foreign_user_id_when_authed():
    import asyncio

    rt = _runtime()
    me = _user(uid="u1")

    async def go():
        # 鉴权开启（真实令牌身份）：传入他人 user_id 被忽略，只关自己的记忆
        await memory_router.set_memory_disabled(
            {"user_id": "someone-else", "disabled": True}, current=me, runtime=rt
        )
        assert rt.memory_disabled == {"u1": True}
        st = await memory_router.get_memory_disabled(user_id="someone-else", current=me, runtime=rt)
        assert st["user_id"] == "u1" and st["disabled"] is True

    asyncio.run(go())
