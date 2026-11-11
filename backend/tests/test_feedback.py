"""点赞 / 点踩反馈 + 意图识别 few-shot 匹配 单测。

不依赖完整 app 启动（milvus 等重资源），直接对仓储层 / feedback 路由函数 /
classify_intent 召回逻辑做单元 + 内存模式集成测试。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.nodes.classify_intent import (
    _recall_few_shots,
    build_classify_intent_node,
)
from dmf_agent.api.routers import feedback as feedback_router
from dmf_agent.db.factory import DatabaseBundle
from dmf_agent.schemas.agent import IntentResult
from dmf_agent.schemas.feedback import BadCaseData, FeedbackRequest, FewShotData
from dmf_agent.schemas.session import MessageData


def _bundle() -> DatabaseBundle:
    # 无 engine -> 纯内存模式，所有 repo 为内存实现
    return DatabaseBundle(mode="memory")


async def test_few_shot_repo_add_and_recall():
    db = _bundle()
    ex = await db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="metric_query",
            category="intention", question="查一下近7天的GMV趋势", answer="A", score=1,
        )
    )
    assert ex.id >= 1
    # 同消息重复点赞 -> 累加 score（幂等，不重复入库）
    ex2 = await db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="metric_query",
            category="intention", question="查一下近7天的GMV趋势", answer="A", score=1,
        )
    )
    assert ex2.score == 2
    got = await db.few_shot_repo.get_by_message("m1")
    assert got is not None and got.intent == "metric_query"
    all_ex = await db.few_shot_repo.list_all()
    assert len(all_ex) == 1  # 仍是 1 条（幂等）


async def test_bad_case_repo_add():
    db = _bundle()
    case = await db.bad_case_repo.add(
        BadCaseData(
            id=0, session_id="s1", message_id="m2", intent="metric_query",
            question="各分行存款排名", answer="B", reason="不准",
        )
    )
    assert case.id >= 1
    assert (await db.bad_case_repo.get_by_message("m2")).reason == "不准"


async def test_feedback_router_like_and_dislike():
    db = _bundle()
    # 准备一条助手消息（node_trace 含原始问句，fallback 取 intent）
    await db.message_repo.add(
        MessageData(
            id="m-like", session_id="s1", role="assistant", content="点赞回答正文",
            node_trace={"question": "查一下近7天的GMV趋势", "intent": "metric_query"},
        )
    )
    await db.message_repo.add(
        MessageData(
            id="m-dis", session_id="s1", role="assistant", content="点踩回答正文",
            node_trace={"question": "各分行存款排名", "intent": "metric_query"},
        )
    )
    runtime = SimpleNamespace(db=db)

    # 点赞
    r1 = await feedback_router.submit_feedback(
        FeedbackRequest(message_id="m-like", type="like"), runtime
    )
    assert r1["ok"] and r1["type"] == "like"
    fs = await db.few_shot_repo.get_by_message("m-like")
    assert fs is not None and fs.question == "查一下近7天的GMV趋势" and fs.intent == "metric_query"

    # 点踩
    r2 = await feedback_router.submit_feedback(
        FeedbackRequest(message_id="m-dis", type="dislike", reason="不准"), runtime
    )
    assert r2["ok"] and r2["type"] == "dislike"
    bc = await db.bad_case_repo.get_by_message("m-dis")
    assert bc is not None and bc.reason == "不准"

    # 批量状态查询
    status = await feedback_router.feedback_status("m-like,m-dis,ghost", runtime)
    assert status.status == {"m-like": "like", "m-dis": "dislike"}


async def test_feedback_router_unknown_message_404():
    import pytest as _pytest
    from fastapi import HTTPException

    db = _bundle()
    runtime = SimpleNamespace(db=db)
    with _pytest.raises(HTTPException):
        await feedback_router.submit_feedback(
            FeedbackRequest(message_id="ghost", type="like"), runtime
        )


async def test_feedback_cancel_and_switch():
    """取消反馈（再点一次同类型）与类型切换：一消息仅一种状态。"""
    db = _bundle()
    await db.message_repo.add(
        MessageData(
            id="m-x", session_id="s1", role="assistant", content="回答正文",
            node_trace={"question": "近7天GMV趋势", "intent": "metric_query"},
        )
    )
    runtime = SimpleNamespace(db=db)

    # 点赞 -> 撤销：样本与 L3 副本一并清除
    await feedback_router.submit_feedback(FeedbackRequest(message_id="m-x", type="like"), runtime)
    assert (await db.few_shot_repo.get_by_message("m-x")) is not None
    r_cancel = await feedback_router.submit_feedback(
        FeedbackRequest(message_id="m-x", type="cancel"), runtime
    )
    assert r_cancel["type"] == "cancel"
    assert (await db.few_shot_repo.get_by_message("m-x")) is None
    l3 = [i for i in await db.domain_memory_repo.list_all() if i.question == "近7天GMV趋势"]
    assert l3 == []
    status = await feedback_router.feedback_status("m-x", runtime)
    assert status.status.get("m-x") is None

    # 点踩 -> 撤销
    await feedback_router.submit_feedback(FeedbackRequest(message_id="m-x", type="dislike"), runtime)
    await feedback_router.submit_feedback(FeedbackRequest(message_id="m-x", type="cancel"), runtime)
    assert (await db.bad_case_repo.get_by_message("m-x")) is None

    # 点赞 -> 切点踩：few-shot 清、bad-case 进；状态唯一
    await feedback_router.submit_feedback(FeedbackRequest(message_id="m-x", type="like"), runtime)
    await feedback_router.submit_feedback(FeedbackRequest(message_id="m-x", type="dislike"), runtime)
    assert (await db.few_shot_repo.get_by_message("m-x")) is None
    assert (await db.bad_case_repo.get_by_message("m-x")) is not None
    status = await feedback_router.feedback_status("m-x", runtime)
    assert status.status["m-x"] == "dislike"


async def test_recall_few_shots_empty_when_no_repo():
    deps = AgentDeps(settings=None, get_chat_model=lambda: None, few_shot_repo=None)
    assert await _recall_few_shots(deps, "任意问题") == ""


async def test_classify_intent_injects_few_shot(monkeypatch):
    db = _bundle()
    await db.few_shot_repo.add(
        FewShotData(
            id=0, session_id="s1", message_id="m1", intent="metric_query",
            category="intention", question="查一下近7天的GMV趋势", answer="A", score=1,
        )
    )
    deps = AgentDeps(
        settings=None, get_chat_model=lambda: object(), few_shot_repo=db.few_shot_repo
    )

    captured = {}

    async def fake_invoke(llm, schema, messages):
        captured["messages"] = messages
        return IntentResult(intent="metric_query", skills=[], multi_query=False, reason="x")

    monkeypatch.setattr(
        "dmf_agent.agents.metric_agent.nodes.classify_intent.astructured_invoke",
        fake_invoke,
    )

    node = build_classify_intent_node(deps)
    state = {"messages": [HumanMessage(content="查一下近7天GMV趋势是多少")]}
    result = await node(state)
    assert result["intent"] == "metric_query"
    # prompt 中应包含从 few-shot 库召回的相似已验证样本
    human = next(m for m in captured["messages"] if isinstance(m, HumanMessage))
    assert "查一下近7天的GMV趋势" in human.content
    assert "已验证样本" in human.content
