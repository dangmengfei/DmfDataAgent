"""会话级串行化闸（`services/session_lock.py`）测试。

覆盖：
1. 进程内锁：同会话串行、不同会话并行、等锁超时报忙、空会话/关闭不互斥；
2. Redis 分布式锁：跨实例互斥、竞争失败报忙、Redis 异常降级为进程内锁（不阻断）。

零外部依赖：Redis 用内存里的 FakeRedisLock 替身，不连真实 Redis。
"""

from __future__ import annotations

import asyncio

import pytest

from dmf_agent.services.session_lock import SessionBusy, SessionLock

# 让「让出事件循环、让被创建的 task 真正跑到阻塞点」更稳一点
_YIELD = asyncio.sleep(0)


class FakeRedisLock:
    """`RedisClient` 锁原语的最小替身。"""

    def __init__(self) -> None:
        self._keys: dict = {}
        self.fail = False

    async def set_nx(self, key, value, ttl=0):
        if self.fail:
            return None  # 模拟 Redis 异常 → 调用方降级
        if key in self._keys:
            return False
        self._keys[key] = value
        return True

    async def compare_and_delete(self, key, value):
        if self._keys.get(key) == value:
            del self._keys[key]
            return True
        return False


# ---------------------------------------------------------------------------
# 1. 进程内锁
# ---------------------------------------------------------------------------
async def test_local_lock_serializes_same_session():
    lock = SessionLock(wait_seconds=5.0)
    order: list = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with lock.hold("s"):
            order.append("first_in")
            entered.set()
            await release.wait()
            order.append("first_out")

    async def second():
        await entered.wait()
        async with lock.hold("s"):
            order.append("second_in")

    f = asyncio.create_task(first())
    await entered.wait()
    s = asyncio.create_task(second())
    await _YIELD
    release.set()
    await asyncio.gather(f, s)
    # second 在 first 释放后才进入 → 串行生效（否则 second_in 会插在 first_out 前）
    assert order == ["first_in", "first_out", "second_in"]


async def test_local_lock_allows_different_sessions():
    lock = SessionLock(wait_seconds=5.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def a():
        async with lock.hold("a"):
            entered.set()
            await release.wait()

    ta = asyncio.create_task(a())
    await entered.wait()

    async def b():
        async with lock.hold("b"):
            return

    await asyncio.wait_for(asyncio.create_task(b()), timeout=0.5)
    release.set()
    await ta


async def test_local_lock_immediate_busy():
    """wait_seconds=0：锁被占时立即报忙，不等待。"""
    lock = SessionLock(wait_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(SessionBusy):
        async with lock.hold("s"):
            pass
    release.set()
    await h


async def test_local_lock_wait_times_out():
    """正数等待窗口超时后同样报忙。"""
    lock = SessionLock(wait_seconds=0.05)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(SessionBusy):
        async with lock.hold("s"):
            pass
    release.set()
    await h


async def test_empty_session_id_not_locked():
    lock = SessionLock(wait_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    async with lock.hold(""):
        pass
    release.set()
    await h


async def test_disabled_not_locked():
    lock = SessionLock(enabled=False, wait_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    async with lock.hold("s"):
        pass
    release.set()
    await h


# ---------------------------------------------------------------------------
# 2. Redis 分布式锁
# ---------------------------------------------------------------------------
async def test_redis_lock_excludes_across_instances():
    """同一 Redis 可见两个实例：A 持有锁期间，B 竞争失败报忙；释放后可再拿。"""
    redis = FakeRedisLock()
    a = SessionLock(redis=redis, wait_seconds=0)
    b = SessionLock(redis=redis, wait_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with a.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(SessionBusy):
        async with b.hold("s"):
            pass
    release.set()
    await h
    async with b.hold("s"):
        pass


async def test_redis_error_falls_back_to_local():
    """set_nx 返回 None（Redis 异常）→ 降级放行，不报忙、不阻断对话。"""
    redis = FakeRedisLock()
    redis.fail = True
    lock = SessionLock(redis=redis, wait_seconds=0)
    async with lock.hold("s"):
        pass


async def test_redis_error_still_serializes_locally():
    """Redis 异常降级后，进程内锁仍兜底保证单实例内互斥。"""
    redis = FakeRedisLock()
    redis.fail = True
    lock = SessionLock(redis=redis, wait_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold("s"):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(SessionBusy):
        async with lock.hold("s"):
            pass
    release.set()
    await h


# ---------------------------------------------------------------------------
# 3. 服务级接线：astream_chat 忙路径产出 E5005 且不落库、不进主图
# ---------------------------------------------------------------------------
async def test_astream_chat_busy_emits_e5005_without_touching_graph_or_store():
    from dmf_agent.config.settings import Settings
    from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
    from dmf_agent.services.chat_service import ChatService

    lock = SessionLock(wait_seconds=0)
    session_repo = InMemorySessionRepository()
    message_repo = InMemoryMessageRepository()
    session = await session_repo.create(user_id="u1", title="t", tenant_id="t1")

    class _NeverGraph:
        async def astream(self, *a, **k):
            raise AssertionError("会话忙的请求绝不能进主图")

    svc = ChatService(
        graph=_NeverGraph(), session_repo=session_repo, message_repo=message_repo,
        settings=Settings(), session_lock=lock,
    )

    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with lock.hold(session.id):
            entered.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await entered.wait()
    events = [e async for e in svc.astream_chat(
        session_id=session.id, user_id="u1", message="查GMV",
    )]
    release.set()
    await h

    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "error"
    assert ev["session_id"] == session.id
    assert ev["error_info"]["code"] == "E5005"
    assert ev["error_info"]["retryable"] is True
    # 不落库：该会话下没有任何消息（忙碌不是一轮对话）
    assert await message_repo.list_by_session(session.id) == []