"""会话级串行化闸：让同一 session_id 的对话轮次互斥。

背景：同一 user_id 多人（或同一人多标签页）共用同一个会话框时，对话服务里若干共享
资源是「按 session_id 覆盖写」的 —— 消息历史（messages）按 `(created_at, id)` 追加、
滚动摘要（memory_summaries）按 session_id 整条 upsert、结构化对话状态
（conversation_states）last-writer-wins。并发轮次会：

- user / assistant 消息交错落库，历史顺序错乱（下一轮组装上下文、确认态组合、
  「编辑重发替换该轮」的截断点都会取错）；
- 滚动压缩重复触发，后写覆盖先写，摘要可能回退到覆盖更少的版本；
- 对话状态互相覆盖，下一轮确定性指代消解指错。

因此把「同一 session_id 的一轮对话」串行化：先到先得，后到的等锁，超时即报
`SessionBusy`（由上层转成「会话正在回复中」的结构化错误，不落库、不污染历史）。

后端同一套接口可切换：

- **进程内 asyncio.Lock**（默认兜底，总是可用）：单实例足够；
- **Redis 分布式锁**（`redis` 传入时额外叠加）：多实例部署下跨进程互斥。

Redis 不可用 / 未配置 / 操作异常一律回落进程内锁 —— 与全项目的降级矩阵一致，绝不因
Redis 抖动堵死对话主链路；代价是 Redis 故障期间只保证单实例内互斥（已有进程内锁兜底），
跨实例可能短暂重叠，属可接受的降级。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

logger = logging.getLogger(__name__)


class SessionBusy(Exception):
    """同一会话已有另一轮在跑，等锁超时或竞争失败。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"会话 {session_id} 正在被另一轮对话占用")
        self.session_id = session_id


def _lock_key(session_id: str) -> str:
    return f"dmf:session:lock:{session_id}"


class SessionLock:
    """会话轮次互斥锁（进程内锁 + 可选 Redis 分布式锁叠加）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        wait_seconds: float = 2.0,
        ttl_seconds: int = 600,
        redis: Any = None,
        poll_interval: float = 0.05,
    ) -> None:
        self._enabled = enabled
        # <= 0 表示「不等待，立即报忙」
        self._wait = max(float(wait_seconds), 0.0)
        # Redis 锁 TTL：需大于单轮最长耗时（LLM 超时 × 重试 + 取数），否则锁会中途过期
        self._ttl = max(int(ttl_seconds), 1)
        self._redis = redis
        self._poll = max(float(poll_interval), 0.01)
        # {session_id: [asyncio.Lock, 引用计数]}；计数归零且锁空闲时移除，避免无限增长
        self._local: Dict[str, List[Any]] = {}

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        """占用该会话的锁；新会话（空 id）/ 未启用时直接放行。等锁超时抛 `SessionBusy`。"""
        if not self._enabled or not session_id:
            yield
            return

        async with self._hold_local(session_id):
            if self._redis is None:
                yield
                return

            key = _lock_key(session_id)
            token = uuid.uuid4().hex
            acquired = await self._acquire_redis(key, token)
            if acquired is False:
                raise SessionBusy(session_id)
            try:
                yield
            finally:
                # acquired is None 表示 Redis 异常已降级放行，无需释放（也没拿到）
                if acquired is True:
                    await self._release_redis(key, token)

    @asynccontextmanager
    async def _hold_local(self, session_id: str) -> AsyncIterator[None]:
        entry = self._local.get(session_id)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._local[session_id] = entry
        entry[1] += 1
        lock: asyncio.Lock = entry[0]
        try:
            try:
                if self._wait <= 0:
                    # 不等待：锁被占立即报忙（locked() 与 acquire() 之间无 await 点，无竞争）
                    if lock.locked():
                        raise SessionBusy(session_id)
                    await lock.acquire()
                else:
                    await asyncio.wait_for(lock.acquire(), timeout=self._wait)
            except asyncio.TimeoutError:
                raise SessionBusy(session_id) from None
            try:
                yield
            finally:
                lock.release()
        finally:
            entry[1] -= 1
            if entry[1] <= 0 and not lock.locked():
                # 只剩我们自己（刚释放、无等待者）时才回收，避免把别人正等的锁摘掉
                if self._local.get(session_id) is entry:
                    self._local.pop(session_id, None)

    async def _acquire_redis(self, key: str, token: str):
        """返回 True=拿到 / False=竞争失败（他人持有）/ None=Redis 异常（应降级放行）。"""
        deadline = time.monotonic() + self._wait
        while True:
            res = await self._redis.set_nx(key, token, ttl=self._ttl)
            if res is True:
                return True
            if res is None:
                return None
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._poll)

    async def _release_redis(self, key: str, token: str) -> None:
        await self._redis.compare_and_delete(key, token)