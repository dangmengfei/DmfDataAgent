"""查询结果缓存（P2-1）：同参数 5min TTL 内存缓存，命中直出。

设计要点：
- **零依赖**：纯进程内 dict + 锁，不引入 Redis / 外部组件（PRD NFR 要求无 Redis 依赖）。
- **跨请求持久**：引擎按租户/请求频繁重建（semantic_scope.py / app.py），故缓存必须是
  **模块级单例**，不能挂在引擎实例上，否则每次请求都换新实例 -> 缓存永远不命中。
- **命名空间隔离**：key 纳入 layer 命名空间（manifest 路径），避免不同语义层定义串缓存。
- **租户隔离**：row_scopes（行权限 FR13 P6）纳入 key，不同权限范围不会命中彼此缓存。
- **安全降级**：TTL<=0 或 max_size<=0 视为关闭；任意异常都静默放过，绝不阻断取数主链路。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional


class _Entry:
    __slots__ = ("payload", "expire_at")

    def __init__(self, payload: Dict[str, Any], ttl: float) -> None:
        self.payload = payload
        self.expire_at = time.monotonic() + ttl


class QueryCache:
    """进程内查询结果缓存：命名空间 + 查询参数 + 行权限范围 作为 key。"""

    def __init__(self, ttl_sec: int = 300, max_size: int = 1024) -> None:
        self.ttl = max(0, int(ttl_sec))
        self.max_size = max(0, int(max_size))
        self._store: Dict[str, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _serialize(value: Any) -> str:
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _key(self, namespace: str, source: Any, row_scopes: Optional[Dict[str, Any]]) -> str:
        if hasattr(source, "to_dict"):
            src = source.to_dict()
        elif isinstance(source, dict):
            src = source
        else:
            # MetricQuery 等对象：用 __dict__ 归一（json sort_keys + default=str 保证确定性）
            try:
                src = vars(source)
            except TypeError:
                src = str(source)
        src_str = self._serialize(src)
        if row_scopes:
            scope = {
                k: sorted(v) if isinstance(v, (set, list, tuple)) else v
                for k, v in row_scopes.items()
            }
            scope_str = self._serialize(scope)
        else:
            scope_str = ""
        raw = f"{namespace}||{src_str}||{scope_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(
        self, namespace: str, source: Any, row_scopes: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if self.ttl <= 0 or self.max_size <= 0:
            return None
        key = self._key(namespace, source, row_scopes)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expire_at:
                self._store.pop(key, None)
                return None
            return entry.payload

    def put(
        self,
        namespace: str,
        source: Any,
        row_scopes: Optional[Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> None:
        if self.ttl <= 0 or self.max_size <= 0:
            return
        key = self._key(namespace, source, row_scopes)
        with self._lock:
            if len(self._store) >= self.max_size:
                self._evict()
            self._store[key] = _Entry(dict(payload), self.ttl)

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now > e.expire_at]
        for k in expired:
            self._store.pop(k, None)
        if len(self._store) >= self.max_size and self._store:
            self._store.pop(next(iter(self._store)))

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            live = sum(1 for e in self._store.values() if now <= e.expire_at)
            return {
                "enabled": self.ttl > 0 and self.max_size > 0,
                "ttl_sec": self.ttl,
                "max_size": self.max_size,
                "entries": len(self._store),
                "live": live,
            }


# 模块级单例：跨引擎实例与跨请求共享（见本文件顶部说明）。
_QUERY_CACHE: Optional[QueryCache] = None
_CACHE_INIT = False


def get_query_cache(ttl_sec: int = 300, max_size: int = 1024) -> Optional[QueryCache]:
    """惰性初始化模块级缓存单例。

    `ttl_sec<=0` 或 `max_size<=0` 时返回 None（视为关闭缓存），且后续调用保持关闭，
    直到进程重启——配置在进程内静态，无需反复重建。
    """
    global _QUERY_CACHE, _CACHE_INIT
    if _CACHE_INIT:
        return _QUERY_CACHE
    _CACHE_INIT = True
    if ttl_sec > 0 and max_size > 0:
        _QUERY_CACHE = QueryCache(ttl_sec=ttl_sec, max_size=max_size)
    else:
        _QUERY_CACHE = None
    return _QUERY_CACHE


def reset_query_cache_for_testing(ttl_sec: int = 300, max_size: int = 1024) -> None:
    """测试用：重建模块级缓存单例（打破惰性初始化状态）。"""
    global _QUERY_CACHE, _CACHE_INIT
    _CACHE_INIT = True
    _QUERY_CACHE = QueryCache(ttl_sec=ttl_sec, max_size=max_size) if (ttl_sec > 0 and max_size > 0) else None


__all__ = ["QueryCache", "get_query_cache", "reset_query_cache_for_testing"]
