"""多轮对话状态管理（P0-6）单测。

覆盖：
- 仓储三同步：InMemory 的 get / upsert / delete / list_recent；
- ConversationStateService：快照合并（追问仅补维度/过滤，沿用上一轮指标）、状态机
  （active ↔ awaiting_clarification）、上下文读取（load_context）、重置（reset）；
- 要素抽取集成：context_entities 的提示格式化（_format_context_hint）与规则路径默认值
  合并（_merge_context_defaults），供确定性指代消解；
- 路由层：GET /sessions/{id}/state 与 POST /sessions/{id}/state/reset（auth 关闭=合成 admin）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dmf_agent.api.deps import get_runtime
from dmf_agent.api.routers import sessions as sessions_router
from dmf_agent.agents.metric_agent.nodes.extract_elements import (
    _format_context_hint,
    _merge_context_defaults,
)
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.db.memory import InMemoryConversationStateRepository
from dmf_agent.schemas.session import ConversationStateData
from dmf_agent.services.conversation_state import ConversationStateService


# ---------------- 仓储三同步 ----------------


@pytest.mark.asyncio
async def test_repo_three_sync():
    repo = InMemoryConversationStateRepository()
    assert await repo.get("s1") is None

    s = ConversationStateData(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query", active_metrics=["存款余额"], active_dimensions=["region"],
        time_range={"granularity": "month"}, topic="存款查询", turn_count=1,
    )
    saved = await repo.upsert(s)
    assert saved.session_id == "s1"
    got = await repo.get("s1")
    assert got is not None and got.active_metrics == ["存款余额"] and got.turn_count == 1

    # upsert 覆盖（同主键）
    s2 = ConversationStateData(session_id="s1", tenant_id="t1", active_metrics=["贷款余额"], turn_count=2)
    await repo.upsert(s2)
    got2 = await repo.get("s1")
    assert got2.active_metrics == ["贷款余额"] and got2.turn_count == 2

    # list_recent 按租户过滤
    await repo.upsert(ConversationStateData(session_id="s2", tenant_id="t2"))
    recent = await repo.list_recent(tenant_id="t1")
    assert {r.session_id for r in recent} == {"s1"}

    # delete
    assert await repo.delete("s1") is True
    assert await repo.delete("s1") is False
    assert await repo.get("s1") is None


# ---------------- 服务：快照合并 + 状态机 ----------------


@pytest.mark.asyncio
async def test_snapshot_merge_and_state_machine():
    repo = InMemoryConversationStateRepository()
    svc = ConversationStateService(repo)

    # 第一轮：完整解析
    s1 = await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={
            "active_metrics": ["存款余额"],
            "active_dimensions": ["region"],
            "active_filters": {"org": "总行"},
            "time_range": {"granularity": "month"},
        },
        query_original="存款余额按地区",
        query_rewritten="存款余额 按 地区",
    )
    assert s1.status == "active"
    assert s1.turn_count == 1
    assert s1.active_metrics == ["存款余额"]

    # 第二轮：追问「按产品拆分」（只补维度，未提指标 -> 沿用上一轮指标）
    s2 = await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={
            "active_metrics": [],
            "active_dimensions": ["product"],
            "active_filters": {},
            "time_range": {},
        },
        query_original="按产品拆分",
        query_rewritten="按 产品 拆分",
    )
    assert s2.turn_count == 2
    # 指标沿用、维度更新、过滤同样沿用（空缺不回退）
    assert s2.active_metrics == ["存款余额"]
    assert s2.active_dimensions == ["product"]
    assert s2.active_filters == {"org": "总行"}
    assert s2.time_range == {"granularity": "month"}

    # 挂起澄清 -> awaiting_clarification，且不计数
    s3 = await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={},
        confirmation={"problems": ["请确认口径"], "original_query": "存款余额"},
    )
    assert s3.status == "awaiting_clarification"
    assert s3.turn_count == 2  # 未 +1
    assert s3.pending_clarification.get("original_query") == "存款余额"


@pytest.mark.asyncio
async def test_load_context_and_reset():
    repo = InMemoryConversationStateRepository()
    svc = ConversationStateService(repo)

    # 无状态 -> load_context 返回 None
    assert await svc.load_context("s1") is None

    await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={"active_metrics": ["存款余额"], "active_dimensions": ["region"]},
        query_original="存款余额",
    )
    ctx = await svc.load_context("s1")
    assert ctx is not None
    assert ctx["active_metrics"] == ["存款余额"]
    assert ctx["turn_count"] == 1

    # 重置 -> 清空
    assert await svc.reset("s1") is True
    assert await svc.get("s1") is None
    assert await svc.load_context("s1") is None
    # 重复 reset 返回 False
    assert await svc.reset("s1") is False


# ---------------- 要素抽取集成（确定性指代消解） ----------------


def test_format_context_hint():
    empty = _format_context_hint({"context_entities": {}})
    assert empty == ""
    hint = _format_context_hint({
        "context_entities": {
            "active_metrics": ["存款余额"],
            "active_dimensions": ["region"],
            "time_range": {"granularity": "month"},
            "topic": "存款查询",
        }
    })
    assert "存款余额" in hint and "region" in hint and "month" in hint


def test_merge_context_defaults():
    # 本轮只问维度，未提指标 -> 沿用上一轮指标/过滤
    # 对话状态里 filters 归一为 {字段: {op, values}}（ChatService 落库时转换）
    state = {
        "context_entities": {
            "active_metrics": ["存款余额"],
            "active_dimensions": ["region"],
            "active_filters": {"org": {"op": "=", "values": ["总行"]}},
            "time_range": {"granularity": "month"},
        }
    }
    elements = {
        "metrics": [], "dimensions": ["product"], "filters": [],
        "time_range": {}, "order_by": None,
    }
    merged = _merge_context_defaults(elements, state)
    assert merged["metrics"] == ["存款余额"]        # 沿用
    assert merged["dimensions"] == ["product"]        # 本轮优先
    assert merged["filters"] == [
        {"field": "org", "operator": "=", "values": ["总行"]}
    ]
    assert merged["time_range"] == {"granularity": "month"}

    # 本轮已显式指定 -> 不覆盖
    state2 = {"context_entities": {"active_metrics": ["存款余额"]}}
    merged2 = _merge_context_defaults(
        {"metrics": ["贷款余额"], "dimensions": [], "filters": [], "time_range": {}},
        state2,
    )
    assert merged2["metrics"] == ["贷款余额"]


# ---------------- 路由层 ----------------


async def _build_client_and_session() -> tuple[TestClient, str, DatabaseBundle]:
    """构造带会话的 TestClient（对话状态路由需要会话归属校验通过）。

    用 `await` 建会话，避免在已有事件循环内调用 run_until_complete 报错。
    """
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
async def test_api_state_and_reset():
    client, sid, _ = await _build_client_and_session()

    # 无状态 -> 200 + null
    r = client.get(f"/sessions/{sid}/state")
    assert r.status_code == 200
    assert r.json() is None

    # 重置一个不存在的状态 -> reset True, existed False
    r = client.post(f"/sessions/{sid}/state/reset")
    assert r.status_code == 200
    assert r.json() == {"reset": True, "existed": False}

    # 不存在的会话 -> 404（scope 校验）
    r = client.get("/sessions/nope/state")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_state_roundtrip_via_service():
    client, sid, db = await _build_client_and_session()
    # 写入状态（模拟 ChatService 落库），注入同一 repo 到路由 runtime
    repo = InMemoryConversationStateRepository()
    db.conversation_state_repo = repo
    await ConversationStateService(repo).snapshot_from(
        session_id=sid, tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={"active_metrics": ["存款余额"], "active_dimensions": ["region"]},
        query_original="存款余额",
    )

    r = client.get(f"/sessions/{sid}/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_metrics"] == ["存款余额"]
    assert body["turn_count"] == 1

    # reset 清空
    r = client.post(f"/sessions/{sid}/state/reset")
    assert r.status_code == 200 and r.json()["existed"] is True
    assert client.get(f"/sessions/{sid}/state").json() is None


# ---------------- ChatService 端到端接线（防回归：快照落库 + 下轮读取） ----------------


@pytest.mark.asyncio
async def test_chatservice_persists_state_after_successful_turn():
    """一轮成功查询后，结构化对话状态必须落库（回归 add_many 误嵌套 conv 守卫的同类问题）。"""
    from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
    from dmf_agent.config.settings import ObservabilitySettings, Settings
    from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
    from dmf_agent.recall import MetricRecaller
    from dmf_agent.semantic.layer import SemanticLayer
    from dmf_agent.services.chat_service import ChatService

    settings = Settings(observability=ObservabilitySettings(enabled=False))
    graph = build_metric_agent_graph(
        AgentDeps(
            settings=settings,
            get_chat_model=lambda: None,  # 走规则路径，无需 LLM
            semantic_layer=SemanticLayer(),
            recaller=MetricRecaller(),
        )
    )
    conv_repo = InMemoryConversationStateRepository()
    service = ChatService(
        graph=graph,
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
        conversation_state_repo=conv_repo,
    )

    result = await service.chat(session_id=None, user_id="u1", message="查一下近7天的GMV趋势")
    assert result.get("session_id")

    state = await conv_repo.get(result["session_id"])
    # 成功轮次已快照：turn_count 自增、状态 active、记录了最近问句
    assert state is not None
    assert state.status == "active"
    assert state.turn_count == 1
    assert state.last_query

    # 下一轮上下文可被读取（service.load_context 走的就是注入路径）
    ctx = await ConversationStateService(conv_repo).load_context(result["session_id"])
    assert ctx is not None and ctx["turn_count"] == 1


# ---------------- FR4 反问澄清次数上限 ----------------


@pytest.mark.asyncio
async def test_snapshot_clarification_count_increment_and_reset():
    """澄清序列内每追问一次计数 +1；成功解决后清零（FR4）。"""
    repo = InMemoryConversationStateRepository()
    await repo.upsert(
        ConversationStateData(
            session_id="s1",
            clarification_count=2,
            status="awaiting_clarification",
            pending_clarification={"problems": ["缺维度"], "original_query": "q"},
        )
    )
    svc = ConversationStateService(repo)

    # 本轮回填确认（继续追问）-> 计数 +1
    await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        confirmation={"problems": ["缺维度"], "original_query": "q"},
    )
    st = await repo.get("s1")
    assert st.clarification_count == 3 and st.status == "awaiting_clarification"

    # 下一轮成功解决（无 confirmation）-> 清零、状态回到 active
    await svc.snapshot_from(
        session_id="s1", tenant_id="t1", user_id="u1",
        intent="metric_query",
        query_elements={"active_metrics": ["存款余额"]},
        query_original="存款余额",
    )
    st2 = await repo.get("s1")
    assert st2.clarification_count == 0 and st2.status == "active"


@pytest.mark.asyncio
async def test_ask_clarification_caps_at_max_turns():
    """达到澄清上限后不再追问，给出优雅终止文案（不返回 confirmation）。"""
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.ask_clarification import (
        build_ask_clarification_node,
    )
    from dmf_agent.config.settings import AgentSettings, Settings

    repo = InMemoryConversationStateRepository()
    # 已追问到上限（max=2，prior=2）
    await repo.upsert(
        ConversationStateData(
            session_id="s1",
            clarification_count=2,
            status="awaiting_clarification",
            pending_clarification={"problems": ["缺维度"], "original_query": "x"},
        )
    )
    deps = AgentDeps(
        settings=Settings(agent=AgentSettings(max_clarification_turns=2)),
        get_chat_model=lambda: None,
        conv_state_repo=repo,
    )
    node = build_ask_clarification_node(deps)
    out = await node(
        {"validation": {"ok": False, "problems": ["缺维度"]}, "session_id": "s1"}
    )
    assert "confirmation" not in out
    assert "不再追问" in out["final_answer"]


@pytest.mark.asyncio
async def test_ask_clarification_within_cap_returns_confirmation():
    """未达上限时正常追问（返回 confirmation 供下一轮组合）。"""
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.ask_clarification import (
        build_ask_clarification_node,
    )
    from dmf_agent.config.settings import AgentSettings, Settings

    repo = InMemoryConversationStateRepository()
    # prior=0 < max=2 -> 正常追问
    deps = AgentDeps(
        settings=Settings(agent=AgentSettings(max_clarification_turns=2)),
        get_chat_model=lambda: None,  # 降级模板路径，无需 LLM
        conv_state_repo=repo,
    )
    node = build_ask_clarification_node(deps)
    out = await node(
        {"validation": {"ok": False, "problems": ["缺维度"]}, "session_id": "s1"}
    )
    assert "confirmation" in out and out["confirmation"]["problems"] == ["缺维度"]



