"""跨进程租户缓存失效（T198b）：Redis pub/sub 广播 → 其它 worker 本地即时失效。

不依赖真实 Redis：用进程内 broker 模拟 pub/sub 广播，验证
- 实例 A 调 ``invalidate`` 会经 Redis 广播；
- 订阅中的实例 B 收到广播后本地缓存被即时清空（无需等 1s probe TTL）；
- 无 Redis 时退化为纯进程内失效（不抛错）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


from dmf_agent.services.semantic_scope import SemanticScopeService


class _Broker:
    """进程内 pub/sub 广播器：subscribe 注册监听，publish 推给所有监听。"""

    def __init__(self) -> None:
        self.subs: list = []

    def register(self, ps) -> None:
        self.subs.append(ps)

    async def publish(self, channel: str, payload: str) -> None:
        for ps in list(self.subs):
            await ps._queue.put({"type": "message", "data": payload})


class _FakePubSub:
    def __init__(self, broker: "_Broker") -> None:
        self.broker = broker
        self.channel = None
        self._closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.broker.register(self)

    async def listen(self):
        while not self._closed:
            msg = await self._queue.get()
            if msg is None:
                break
            yield msg

    async def unsubscribe(self, channel: str) -> None:
        self._closed = True

    async def aclose(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass


class _FakeRedisClient:
    def __init__(self, broker: "_Broker") -> None:
        self.broker = broker

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self.broker)

    async def publish(self, channel: str, payload: str) -> int:
        await self.broker.publish(channel, payload)
        return 1


class _FakeRedis:
    def __init__(self, broker: "_Broker") -> None:
        self._client = _FakeRedisClient(broker)


def _make_service(redis_client=None) -> SemanticScopeService:
    settings = SimpleNamespace(semantic=SimpleNamespace(source="yaml"))
    base = SimpleNamespace(chain=[])
    return SemanticScopeService(settings=settings, base=base, redis_client=redis_client)


async def test_cross_process_invalidation_broadcast():
    broker = _Broker()
    svc_a = _make_service(redis_client=_FakeRedis(broker))
    svc_b = _make_service(redis_client=_FakeRedis(broker))

    # B 进程已缓存 t1，A 进程未缓存
    svc_b._cache["t1"] = "cached-in-b"
    assert "t1" in svc_b._cache

    await svc_b.start()  # B 订阅频道
    await asyncio.sleep(0.05)  # 等订阅注册

    svc_a.invalidate("t1")  # A 发布/审批后广播
    await asyncio.sleep(0.15)  # 等广播送达 + B 监听消费

    # B 的本地缓存被即时清空（无需等 1s probe TTL）
    assert "t1" not in svc_b._cache, "跨进程失效未触发：B 的 t1 缓存仍在"
    await svc_b.stop()


async def test_cross_process_invalidation_all_clears_whole_cache():
    broker = _Broker()
    svc_a = _make_service(redis_client=_FakeRedis(broker))
    svc_b = _make_service(redis_client=_FakeRedis(broker))

    svc_b._cache["t1"] = "x"
    svc_b._cache["t2"] = "y"
    await svc_b.start()
    await asyncio.sleep(0.05)

    svc_a.invalidate(None)  # 全量失效广播
    await asyncio.sleep(0.15)

    assert dict(svc_b._cache) == {}, "全量失效广播未清空 B 的缓存"
    await svc_b.stop()


async def test_no_redis_degrades_to_local_only():
    """无 Redis 时，invalidate 仅清本地、不抛错、不广播。"""
    svc = _make_service(redis_client=None)
    svc._cache["t1"] = "x"
    # 即便没有 Redis，本地失效 + 不崩溃
    count = svc.invalidate("t1")
    assert count == 1
    assert "t1" not in svc._cache
    # start/stop 在无 Redis 时也应是空操作
    await svc.start()
    await svc.stop()
