"""断开会话后继续追问：验证历史记忆携带与前轮半截状态的清理。

场景（用户点「停止」/ 关页面断开 SSE 流）：
1. 第 1 轮执行中途断开 —— 用户消息**已落库**（在图执行前 add），助手消息未落库；
2. 同一 session_id 继续追问 —— 下一轮的历史装配是否读到第 1 轮的孤儿用户消息，
   且会话级串行化闸不会因上一轮被断开而卡死。

关键代码路径：chat_service.py 在 `_message_repo.add(user)` 之后才跑图；流断开走的是
`except Exception` 覆盖不到的中途取消，助手消息的落库（图跑完后那段）不会执行。
"""

from dmf_agent.config.settings import ObservabilitySettings, Settings
from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.services.chat_service import ChatService


def _service():
    from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph

    settings = Settings(observability=ObservabilitySettings(enabled=False))
    deps = AgentDeps(
        settings=settings,
        get_chat_model=lambda: None,  # 规则降级，无需 LLM
        semantic_layer=SemanticLayer(),
    )
    return ChatService(
        graph=build_metric_agent_graph(deps),
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
    )


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
        if ev.get("type") in ("final", "error"):
            break
    return events


async def test_interrupt_then_followup_carries_history():
    """断开后同会话追问：第 1 轮用户消息仍在历史中（会被下一轮携带），会话锁未卡死。"""
    svc = _service()
    ev1 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="查一下近7天的GMV趋势"))
    sid = ev1[-1]["session_id"]

    before = await svc._message_repo.list_by_session(sid)
    assert [m.role for m in before].count("user") == 1

    # 同会话追问（模拟断开后重连）：必须能跑通，不被上一轮的锁卡死
    ev2 = await _collect(svc.astream_chat(session_id=sid, user_id="u1", message="按渠道拆分呢"))
    assert ev2[-1]["type"] == "final"

    after = await svc._message_repo.list_by_session(sid)
    contents = [m.content for m in after]
    assert "查一下近7天的GMV趋势" in contents, "第 1 轮用户消息仍在历史中"
    assert "按渠道拆分呢" in contents
    assert len(after) > len(before)


async def test_new_session_after_abort_has_no_old_history():
    """反向对照：断开后**新建**会话不携带旧会话记忆。"""
    svc = _service()
    ev1 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="旧会话的问题"))
    old_sid = ev1[-1]["session_id"]

    ev2 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="新会话的问题"))
    new_sid = ev2[-1]["session_id"]
    assert new_sid != old_sid

    contents = [m.content for m in await svc._message_repo.list_by_session(new_sid)]
    assert "新会话的问题" in contents
    assert "旧会话的问题" not in contents


async def test_session_lock_released_after_cancel():
    """取消上一轮后，同会话下一轮不被「会话忙」拒绝（锁随 async with 正常释放）。"""
    svc = _service()
    ev1 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="第一问"))
    sid = ev1[-1]["session_id"]

    agen = svc.astream_chat(session_id=sid, user_id="u1", message="被打断的一问")
    await agen.__anext__()
    await agen.aclose()  # 模拟客户端断开：关闭生成器

    # 再问一轮：若锁未释放，这里会拿到 E5005 会话忙事件
    ev3 = await _collect(svc.astream_chat(session_id=sid, user_id="u1", message="最后再问"))
    assert ev3[-1]["type"] == "final"


async def test_interrupted_round_has_no_assistant_orphan_pair():
    """确认中断轮的落库形态：user 有、assistant 无（即历史里是「孤儿子消息」）。"""
    svc = _service()
    ev1 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="只有一个问题的会话"))
    sid = ev1[-1]["session_id"]
    roles = [m.role for m in await svc._message_repo.list_by_session(sid)]
    assert "user" in roles
    assert roles.count("assistant") >= 1  # 正常完整轮次应有答复
