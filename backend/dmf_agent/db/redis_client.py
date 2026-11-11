"""Redis 客户端封装（记忆分层 L1 热层 + 热点缓存）。

设计原则：**优雅降级**。任何连接 / 读写异常都被吞掉并返回 None / False，
调用方（记忆仓储、热点缓存）在 Redis 不可用时自动回退 MySQL / 内存主存，
绝不因 Redis 抖动拖慢或打断对话主链路。

仅在 settings.redis.enabled 且 host 可达时返回可用客户端；否则 build_redis_client
返回 None，上层按"无 Redis"处理。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from dmf_agent.config.settings import RedisSettings

logger = logging.getLogger(__name__)


class RedisClient:
    """极简异步封装：JSON 读写 + 删除 + 探活。失败时静默降级。"""

    def __init__(self, settings: RedisSettings) -> None:
        import redis.asyncio as aioredis

        self._settings = settings
        self._client = aioredis.Redis(
            host=settings.host,
            port=settings.port,
            db=settings.db,
            password=settings.password or None,
            socket_timeout=settings.socket_timeout,
            socket_connect_timeout=settings.socket_connect_timeout,
            decode_responses=True,
        )

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001 - Redis 探活失败即视为不可用
            logger.warning("Redis 探活失败，降级为无缓存：%s", exc)
            return False

    async def get_json(self, key: str) -> Optional[Any]:
        try:
            raw = await self._client.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis GET 失败（降级）：%s", exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def set_json(self, key: str, value: Any, ttl: int = 0) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            if ttl and ttl > 0:
                return bool(await self._client.set(key, payload, ex=ttl))
            return bool(await self._client.set(key, payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis SET 失败（降级）：%s", exc)
            return False

    async def delete(self, key: str) -> bool:
        try:
            await self._client.delete(key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis DEL 失败（降级）：%s", exc)
            return False

    async def exists(self, key: str) -> bool:
        try:
            return bool(await self._client.exists(key))
        except Exception:  # noqa: BLE001
            return False

    async def set_nx(self, key: str, value: str, ttl: int = 0) -> Optional[bool]:
        """SET key value NX EX ttl（分布式锁用）。True=拿到 / False=已存在 / None=Redis 异常。

        与其它方法一致：异常静默降级、返回 None，调用方据此回落进程内锁而非当作「被占用」。
        """
        try:
            if ttl and ttl > 0:
                acquired = await self._client.set(key, value, nx=True, ex=ttl)
            else:
                acquired = await self._client.set(key, value, nx=True)
            return bool(acquired)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis SET NX 失败（降级）：%s", exc)
            return None

    async def compare_and_delete(self, key: str, value: str) -> bool:
        """仅当 key 当前值等于 value 时才删除（Lua 原子），避免误删他人刚拿到的锁。"""
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            return bool(await self._client.eval(script, 1, key, value))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 释放锁失败（降级）：%s", exc)
            return False

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def build_redis_client(settings: RedisSettings) -> Optional[RedisClient]:
    """按配置构建 Redis 客户端；未启用或 host 为空返回 None（上层按无缓存处理）。"""
    if not settings.enabled or not settings.host:
        return None
    try:
        return RedisClient(settings)
    except Exception as exc:  # noqa: BLE001 - 构造失败也降级
        logger.warning("Redis 客户端构造失败，降级为无缓存：%s", exc)
        return None


def memory_key(session_id: str) -> str:
    """L1 会话摘要热层 key。"""
    return f"mem:sum:{session_id}"
