"""StarRocks 取数的两道自我保护：`query_timeout`（超时）与 `max_rows`（截断）。

背景：`query_timeout` 此前是**死配置**（定义了 60s 但无人读取）。后果是一条慢查询会
一直占用连接，而连接池上限 `pool_max_size` 默认只有 5 —— 5 条慢 SQL 就能把取数整体
打挂，且没有任何自我保护。现在 `fetch()` 用 `asyncio.wait_for` 整体限时，并在超时后
关闭连接再归还（aiomysql 的 release 对已关闭连接只做除名，不会放回空闲池）。
"""

from __future__ import annotations

import asyncio

import pytest

from dmf_agent.config.settings import StarRocksSettings
from dmf_agent.semantic.starrocks import StarRocksClient, StarRocksError


class _Cursor:
    """模拟有状态的游标：fetchmany 推进读取位置，读尽后返回空（真实游标行为）。"""

    description = [("v",)]

    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._pos = 0

    async def execute(self, sql, params=None):
        # 慢查询的耗时集中在 execute（服务端执行），取数本身很快
        await asyncio.sleep(self._conn.delay)
        self._pos = 0

    async def fetchmany(self, n):
        rows = [[i] for i in range(self._pos, min(self._pos + n, self._conn.total))]
        self._pos += len(rows)
        return rows


class _CursorCtx:
    def __init__(self, conn: "_Conn") -> None:
        self._cur = _Cursor(conn)

    async def __aenter__(self):
        return self._cur

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, delay: float = 0.0, total: int = 3) -> None:
        self.delay = delay
        self.total = total
        self.closed = False

    def close(self):
        self.closed = True

    def cursor(self):
        return _CursorCtx(self)


class _FakePool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.released: list = []

    async def acquire(self):
        return self.conn

    def release(self, conn):  # aiomysql 的 release **不是**协程
        self.released.append(conn)


def _client(pool: _FakePool, *, query_timeout: int = 60, max_rows: int = 200000):
    client = StarRocksClient(
        StarRocksSettings(query_timeout=query_timeout, max_rows=max_rows)
    )

    async def _pool():  # 覆盖实例方法，绕开真实建连
        return pool

    client.pool = _pool  # type: ignore[method-assign]
    return client


async def test_fetch_returns_rows_and_releases_connection():
    pool = _FakePool(_Conn(delay=0))
    out = await _client(pool).fetch("SELECT 1")
    assert out["row_count"] == 3
    assert out["truncated"] is False
    assert pool.released == [pool.conn]         # 连接已归还
    assert pool.conn.closed is False            # 正常路径不关连接


async def test_query_timeout_raises_and_discards_connection():
    """回归点：`query_timeout` 必须真正生效，且脏连接不得回到池里。"""
    pool = _FakePool(_Conn(delay=1.5))  # 单次 execute 就超过 timeout
    with pytest.raises(StarRocksError) as ei:
        await _client(pool, query_timeout=1).fetch("SELECT 1")
    assert "超时" in str(ei.value)
    assert pool.conn.closed is True             # 已关闭（服务端可能仍在跑）
    assert pool.released == [pool.conn]         # 仍走归还流程，但 aiomysql 不会放回空闲池


async def test_max_rows_truncation_still_reported():
    """`max_rows` 与超时互不干扰：截断仍标注 truncated。"""
    pool = _FakePool(_Conn(delay=0, total=100))
    out = await _client(pool, max_rows=5).fetch("SELECT 1")
    assert out["row_count"] == 5
    assert out["truncated"] is True
    assert pool.conn.closed is False            # 截断不是错误，连接照常复用


async def test_settings_defaults_are_sane():
    s = StarRocksSettings()
    assert s.query_timeout > 0
    assert s.max_rows > 0
