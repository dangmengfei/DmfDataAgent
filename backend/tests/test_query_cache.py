"""P2-1 查询结果缓存：命中直出、行权限隔离、命名空间隔离、TTL 过期、关闭路径。"""

import asyncio

from dmf_agent.config.settings import StarRocksSettings
from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.query import MetricQuery
from dmf_agent.semantic.query_cache import (
    QueryCache,
    get_query_cache,
    reset_query_cache_for_testing,
)


class _FakeExecutor:
    """不连库的执行器：记录 fetch 调用次数，返回占位结果。"""

    def __init__(self):
        self.settings = StarRocksSettings()
        self.fetch_calls = 0

    async def fetch(self, sql):
        self.fetch_calls += 1
        return {
            "columns": ["v"],
            "rows": [["1"]],
            "row_count": 1,
            "truncated": False,
            "elapsed_ms": 1.0,
        }


def _make_engine():
    return MetricFlowEngine(SemanticLayer(), _FakeExecutor())


def _q(metrics=("零售存款余额",), granularity="month"):
    return MetricQuery(metrics=list(metrics), granularity=granularity)


def test_cache_hit_avoids_refetch():
    reset_query_cache_for_testing(ttl_sec=300, max_size=128)
    eng = _make_engine()
    src = _q()

    r1 = asyncio.run(eng.query(src))
    calls_after_first = eng.executor.fetch_calls
    assert calls_after_first > 0
    assert r1.get("cached") is False

    r2 = asyncio.run(eng.query(src))
    # 命中缓存：不再触发任何 fetch
    assert eng.executor.fetch_calls == calls_after_first
    assert r2.get("cached") is True
    # 结果与首次一致（SQL 相同）
    assert r2["sql"] == r1["sql"]


def test_row_scope_isolation():
    reset_query_cache_for_testing(ttl_sec=300, max_size=128)
    eng = _make_engine()
    src = _q()

    asyncio.run(eng.query(src))  # 无 scope
    calls_no_scope = eng.executor.fetch_calls

    asyncio.run(eng.query(src, row_scopes={"org": {"branch_A"}}))
    # 不同行权限范围 -> 不同 key -> 未命中 -> 重新取数
    assert eng.executor.fetch_calls > calls_no_scope
    assert eng.executor.fetch_calls == calls_no_scope + (
        eng.executor.fetch_calls - calls_no_scope
    )


def test_namespace_isolation():
    cache = QueryCache(ttl_sec=300, max_size=64)
    src = _q()
    cache.put("ns_a", src, None, {"sql": "A"})
    cache.put("ns_b", src, None, {"sql": "B"})
    assert cache.get("ns_a", src, None) == {"sql": "A"}
    assert cache.get("ns_b", src, None) == {"sql": "B"}


def test_disabled_when_ttl_zero():
    reset_query_cache_for_testing(ttl_sec=0, max_size=64)
    assert get_query_cache(ttl_sec=0) is None

    eng = _make_engine()
    src = _q()
    r1 = asyncio.run(eng.query(src))
    calls1 = eng.executor.fetch_calls
    r2 = asyncio.run(eng.query(src))
    # 关闭缓存：每次都重新取数
    assert calls1 > 0
    assert eng.executor.fetch_calls > calls1
    assert r1.get("cached") is False
    assert r2.get("cached") is False


def test_ttl_expiry():
    reset_query_cache_for_testing(ttl_sec=1, max_size=64)
    cache = get_query_cache()
    src = _q()
    cache.put("ns", src, None, {"sql": "X"})
    assert cache.get("ns", src, None) == {"sql": "X"}
    asyncio.run(asyncio.sleep(1.1))
    assert cache.get("ns", src, None) is None
    assert cache.stats()["live"] == 0


def test_cache_stats_reports_enabled():
    reset_query_cache_for_testing(ttl_sec=300, max_size=16)
    cache = get_query_cache()
    assert cache.stats()["enabled"] is True
    cache.put("ns", _q(), None, {"sql": "Y"})
    assert cache.stats()["live"] == 1
