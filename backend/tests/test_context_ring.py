"""上下文占比圆环 + 手动压缩（FR9 记忆）后端测试。

覆盖：
- HistoryAssembler.context_status（token 口径占比：估算上下文 token / 模型窗口 / 可压缩标记）
- HistoryAssembler.compress_now（手动压缩、回落 retain、幂等、轮数不足、记忆关闭）
- ChatService 薄转发（未装配空态）
- memory router 的 GET context / POST compress（直调路由函数）
"""

import asyncio
from types import SimpleNamespace

from dmf_agent.api.routers import memory as mem_router
from dmf_agent.config.settings import MemorySettings, Settings
from dmf_agent.db.memory import InMemoryMemorySummaryRepository, InMemoryMessageRepository
from dmf_agent.memory.history import HistoryAssembler
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.session import MessageData
from dmf_agent.services.chat_service import ChatService

SID = "s-ring"


def _assembler(settings=None):
    settings = settings or Settings()
    msg_repo = InMemoryMessageRepository()
    sum_repo = InMemoryMemorySummaryRepository()
    assembler = HistoryAssembler(
        message_repo=msg_repo,
        summary_repo=sum_repo,
        settings=settings,
        get_chat_model=lambda: None,  # 模板摘要，确定性
    )
    return assembler, msg_repo, sum_repo


async def _add_rounds(msg_repo, n):
    for i in range(1, n + 1):
        await msg_repo.add(MessageData(id=f"u{i}", session_id=SID, role="user", content=f"问题{i}"))
        await msg_repo.add(MessageData(id=f"a{i}", session_id=SID, role="assistant", content=f"回答{i}"))


# ---------- context_status ----------


async def test_status_token_ratio_by_estimated_tokens():
    assembler, msg_repo, _ = _assembler()  # max_tokens 默认 32768
    st0 = await assembler.context_status(SID)
    assert st0["max_tokens"] == 32768
    assert st0["used_tokens"] == 0 and st0["ratio"] == 0.0

    # 造两段带较长答复的轮次，token 估算应随轮次单调增长
    for i in (1, 2):
        await msg_repo.add(MessageData(id=f"u{i}", session_id=SID, role="user", content=f"问题{i}"))
        await msg_repo.add(
            MessageData(
                id=f"a{i}", session_id=SID, role="assistant",
                content=f"第{i}轮的详细答复，包含较多文本以便估算 token",
            )
        )
    st2 = await assembler.context_status(SID)
    assert st2["memory_enabled"] is True
    assert st2["uncovered_rounds"] == 2
    assert st2["threshold"] == 4
    assert st2["used_tokens"] > 0
    assert 0 < st2["ratio"] <= 1
    assert abs(st2["ratio"] - st2["used_tokens"] / st2["max_tokens"]) < 0.001

    for i in (3, 4):
        await msg_repo.add(MessageData(id=f"u{i}", session_id=SID, role="user", content=f"问题{i}"))
        await msg_repo.add(
            MessageData(
                id=f"a{i}", session_id=SID, role="assistant",
                content=f"第{i}轮的详细答复，包含较多文本以便估算 token",
            )
        )
    st4 = await assembler.context_status(SID)
    assert st4["used_tokens"] > st2["used_tokens"]
    assert st4["ratio"] > st2["ratio"]
    # 轮次要信息仍透出供提示与可压缩判定
    assert st4["uncovered_rounds"] == 4 and st4["compressible"] is True


async def test_status_ratio_clamps_at_one():
    # 把窗口调到极小，验证 ratio 封顶 1.0
    from dmf_agent.config.settings import AgentSettings

    settings = Settings(agent=AgentSettings(context_window_tokens=10))
    assembler, msg_repo, _ = _assembler(settings)
    await _add_rounds(msg_repo, 5)
    st = await assembler.context_status(SID)
    assert st["used_tokens"] > 0
    assert st["ratio"] == 1.0


async def test_status_memory_disabled():
    settings = Settings(memory=MemorySettings(enabled=False))
    assembler, msg_repo, _ = _assembler(settings)
    await _add_rounds(msg_repo, 3)
    st = await assembler.context_status(SID)
    assert st["memory_enabled"] is False
    assert st["ratio"] == 0.0 and st["compressible"] is False


# ---------- compress_now ----------


async def test_compress_now_collapses_to_retain_window():
    assembler, msg_repo, sum_repo = _assembler()
    await _add_rounds(msg_repo, 5)
    result = await assembler.compress_now(SID)
    assert result["compressed"] is True
    assert result["rounds_compressed"] == 3  # uncovered[:-2]
    # 压缩后未压缩轮次回落到 retain，摘要已生成
    st = await assembler.context_status(SID)
    assert st["uncovered_rounds"] == 2
    assert st["has_summary"] is True
    assert st["compressible"] is False
    summary = await sum_repo.get(SID)
    assert summary is not None and "问题1" in summary.summary


async def test_compress_now_idempotent_second_call_noop():
    assembler, msg_repo, _ = _assembler()
    await _add_rounds(msg_repo, 5)
    first = await assembler.compress_now(SID)
    assert first["compressed"] is True
    second = await assembler.compress_now(SID)
    assert second["compressed"] is False
    assert second["reason"] == "nothing_to_compress"
    # 未压缩轮次仍为 retain
    assert (await assembler.context_status(SID))["uncovered_rounds"] == 2


async def test_compress_now_insufficient_rounds():
    assembler, msg_repo, _ = _assembler()
    await _add_rounds(msg_repo, 1)
    result = await assembler.compress_now(SID)
    assert result["compressed"] is False
    assert result["reason"] == "nothing_to_compress"
    assert (await assembler.context_status(SID))["has_summary"] is False


async def test_compress_now_memory_disabled():
    settings = Settings(memory=MemorySettings(enabled=False))
    assembler, msg_repo, _ = _assembler(settings)
    await _add_rounds(msg_repo, 5)
    result = await assembler.compress_now(SID)
    assert result["compressed"] is False
    assert result["reason"] == "memory_disabled"


# ---------- ChatService 薄转发 ----------


async def test_chatservice_forwards_and_empty_when_unwired():
    # 未装配历史装配器 -> 空态/不可压缩
    bare = ChatService(graph=None, session_repo=None, message_repo=None, settings=Settings())
    st = await bare.history_context_status(SID)
    assert st["memory_enabled"] is False
    res = await bare.compress_history(SID)
    assert res["compressed"] is False and res["reason"] == "history_unavailable"

    # 装配后真实转发
    assembler, msg_repo, _ = _assembler()
    await _add_rounds(msg_repo, 5)
    svc = ChatService(
        graph=None, session_repo=None, message_repo=msg_repo,
        settings=Settings(), history_assembler=assembler,
    )
    st = await svc.history_context_status(SID)
    assert st["uncovered_rounds"] == 5 and st["compressible"] is True
    res = await svc.compress_history(SID)
    assert res["compressed"] is True and res["rounds_compressed"] == 3


async def test_compress_history_serialized_by_session_lock():
    """手动压缩走同一把会话锁：会话忙时返回 session_busy（不硬等、不覆盖摘要）。"""
    from dmf_agent.services.session_lock import SessionLock

    assembler, msg_repo, _ = _assembler()
    await _add_rounds(msg_repo, 5)
    lock = SessionLock(wait_seconds=0)
    svc = ChatService(
        graph=None, session_repo=None, message_repo=msg_repo,
        settings=Settings(), history_assembler=assembler, session_lock=lock,
    )

    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold(SID):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    res = await svc.compress_history(SID)
    assert res["compressed"] is False and res["reason"] == "session_busy"
    assert res["uncovered_rounds"] == 5  # 状态照常返回，摘要未被并发压缩破坏

    release.set()
    await h
    res2 = await svc.compress_history(SID)  # 锁释放后可正常压缩
    assert res2["compressed"] is True


# ---------- 路由 ----------


def _user() -> CurrentUser:
    return CurrentUser(id="u1", username="u1", tenant_id="t1", permissions=["*"])


def _router_runtime(assembler):
    svc = ChatService(
        graph=None, session_repo=None, message_repo=None,
        settings=Settings(), history_assembler=assembler,
    )
    return SimpleNamespace(
        chat_service=svc,
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=False)),
    )


async def test_router_get_context_and_post_compress():
    assembler, msg_repo, _ = _assembler()
    await _add_rounds(msg_repo, 5)
    runtime = _router_runtime(assembler)

    st = await mem_router.session_context_status(SID, current=_user(), runtime=runtime)
    assert st["uncovered_rounds"] == 5
    # token 口径（ratio 保留 4 位小数，比较允许舍入误差）
    assert abs(st["ratio"] - st["used_tokens"] / st["max_tokens"]) < 0.001

    res = await mem_router.compress_session_context(SID, current=_user(), runtime=runtime)
    assert res["ok"] is True and res["compressed"] is True and res["rounds_compressed"] == 3

    st2 = await mem_router.session_context_status(SID, current=_user(), runtime=runtime)
    assert st2["uncovered_rounds"] == 2 and st2["has_summary"] is True
