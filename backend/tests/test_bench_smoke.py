"""P2-3：性能压测台冒烟测试。

验证 bench/benchmark.py 的核心可复用单元：
- StubExecutor 返回含 truncated 键（否则 engine 后处理 KeyError -> E1003）
- install_stub_executor 能在真实 runtime 上替换 executor
- compute_stats / analyze 统计与 PRD 达标判定正确
- amain 全流程（mock LLM + stub DB）跑通且全部成功
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


# bench 为命名空间包，确保 backend 在 sys.path（pytest prepend 模式下已注入，这里兜底）
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from bench import benchmark as bench_mod  # noqa: E402


def test_stub_executor_returns_truncated_key():
    stub = bench_mod.StubExecutor()
    out = asyncio.run(stub.fetch("select 1"))
    assert "truncated" in out
    assert out["truncated"] is False
    assert out["columns"] and out["rows"]
    assert asyncio.run(stub.ping()) is True


def test_compute_stats_basic():
    records = [
        {"latency": 0.10, "first_event": 0.02, "ok": True},
        {"latency": 0.20, "first_event": 0.04, "ok": True},
        {"latency": 0.30, "first_event": 0.06, "ok": True},
        {"latency": 1.00, "first_event": 0.10, "ok": True},
    ]
    stats = bench_mod.compute_stats(records, wall_s=1.0)
    assert stats["count"] == 4
    assert stats["ok"] == 4
    assert stats["qps"] == 4.0
    # nearest-rank 分位：4 个样本，p50 落在第 2 大（idx=2）-> 0.30
    assert stats["latency_p50"] == 0.30
    assert stats["latency_p95"] == 1.00
    assert stats["first_event_p95"] == 0.10


def test_compute_stats_records_failure():
    records = [
        {"latency": 0.10, "first_event": None, "ok": True},
        {"latency": 0.50, "first_event": None, "ok": False},
    ]
    stats = bench_mod.compute_stats(records, wall_s=0.5)
    assert stats["ok"] == 1
    assert stats["error"] == 1
    assert stats["first_event_p95"] is None  # 无首事件记录


def test_analyze_pass_flags():
    stats = {
        "latency_p95": 0.05,
        "first_event_p95": 0.03,
        "qps": 150.0,
    }
    cfg = bench_mod.BenchConfig(requests=8, concurrency=8)
    a = bench_mod.analyze(stats, cfg)
    assert a["p95_pass"] is True
    assert a["first_token_pass"] is True
    assert a["prd_qps_target"] == 200
    assert "框架" in a["note"]


async def test_full_bench_via_amain():
    """端到端：装配真实 runtime + stub DB + mock LLM，小并发跑通且全部成功。"""
    cfg = bench_mod.BenchConfig(
        requests=4, concurrency=2, stream=False, stub_db=True, real_llm=False, warmup=1,
    )
    report = await bench_mod.amain(cfg)
    assert report["stub_db_installed"] is True
    stats = report["stats"]
    assert stats["count"] == cfg.requests
    assert stats["ok"] == cfg.requests  # happy-path 全部成功
    assert stats["error"] == 0
    assert stats["qps"] > 0
    # PRD 框架下 P95 与首字均应通过（隔离了真实 LLM/StarRocks）
    assert report["analysis"]["p95_pass"] is True
