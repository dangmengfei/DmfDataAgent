"""数据源后台周期看门狗测试（semantic/probe.py::datasource_watchdog）。

用可控假 metric_engine 脚本化多次 probe() 的返回，驱动看门狗跑若干个短周期后取消，
断言：结果持续写入 runtime.datasource_status、状态翻转时的处理、interval<=0 不启动、
取消时干净退出。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# 环境兼容：导入业务模块期间临时绕开文件 broker。
#
# WorkBuddy shim 在解释器启动期就把 builtins.open / io.open 替换成走文件 broker 的
# 版本（命令行再设 CODEBUDDY_BROKERED_FS_HOOK_ENABLED=0 已太晚）。导入 dmf_agent 会
# 连带 import pydantic / pluggy，它们在装配时用 importlib.metadata.entry_points() 反复
# 读取各安装包的元数据文件；若某次 broker 文件 token 请求无响应，open 会无限等在
# readline 上（表现为 pytest 整批零输出、最终 137）。
#
# 这里取未被 patch 的 C 层 ``_io.open``，仅在 import 窗口内替换，导入完成后立刻还原，
# 因此测试真正执行时其它代码（含沙箱写保护）用的仍是原来的 open。
# ---------------------------------------------------------------------------
import _io as _native_io

_orig_builtin_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open
_orig_io_open = __import__("io").open


def _import_probe():
    import builtins
    import io

    was_brokered = "broker" in getattr(builtins.open, "__name__", "")
    if was_brokered:
        builtins.open = _native_io.open
        io.open = _native_io.open
    try:
        from dmf_agent.semantic.probe import datasource_watchdog
    finally:
        if was_brokered:
            builtins.open = _orig_builtin_open
            io.open = _orig_io_open
    return datasource_watchdog


datasource_watchdog = _import_probe()


def _status(reachable: bool, stale: bool = False) -> Dict[str, Any]:
    summ = {
        "total": 1,
        "reachable_tables": 1 if reachable and not stale else 0,
        "empty_tables": 1 if reachable and stale else 0,
        "stale_tables": 0,
        "failed_tables": 0 if reachable else 1,
    }
    return {
        "reachable": reachable,
        "mode": "starrocks" if reachable else "disabled",
        "detail": "ok" if reachable else "down",
        "elapsed_ms": 1.0,
        "latency_ms": 2.0,
        "version": "8.0",
        "db": "dw",
        "data_as_of": "2026-09-21" if reachable else None,
        "tables": [],
        "summary": summ,
        "checked_at": "2026-09-22T08:00:00",
        "stale": stale,
    }


class ScriptedEngine:
    """按队列依次返回 probe 结果；队列耗尽后重复最后一条。"""

    def __init__(self, statuses: List[Dict[str, Any]]) -> None:
        self._statuses = statuses
        self.probe_calls = 0

    async def probe(self) -> Dict[str, Any]:
        idx = min(self.probe_calls, len(self._statuses) - 1)
        self.probe_calls += 1
        # 给事件循环让出点时间，模拟真实探测
        await asyncio.sleep(0)
        return dict(self._statuses[idx])


def _runtime(engine: ScriptedEngine, interval: Optional[int] = 60):
    settings = SimpleNamespace()
    # 注意：不能用 `interval or 60`，否则传 0（关闭）会被吞成 60。
    settings.effective_starrocks = SimpleNamespace(
        probe_interval=60 if interval is None else interval
    )
    return SimpleNamespace(
        settings=settings,
        metric_engine=engine,
        datasource_status={},
    )


async def _run_cycles(
    runtime: Any,
    interval: float,
    cycles: int,
    expect: Optional[Dict[str, Any]] = None,
) -> None:
    """跑看门狗，等它完成 cycles 次 probe（且状态已写入/符合 expect）后取消。"""
    task = asyncio.create_task(datasource_watchdog(runtime, interval=interval))
    engine = runtime.metric_engine

    def _settled() -> bool:
        if engine.probe_calls < cycles:
            return False
        st = runtime.datasource_status
        if not st:
            return False
        return all(st.get(k) == v for k, v in (expect or {}).items())

    while not _settled():
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_watchdog_writes_status_each_cycle():
    engine = ScriptedEngine([_status(True)])
    runtime = _runtime(engine)
    await _run_cycles(
        runtime, interval=0.01, cycles=3, expect={"reachable": True}
    )

    assert engine.probe_calls >= 3
    # 最后一次结果已写入 runtime
    assert runtime.datasource_status["reachable"] is True
    assert runtime.datasource_status["checked_at"]


@pytest.mark.asyncio
async def test_watchdog_reports_unreachable_baseline():
    # 首轮即不可达：状态应写入且 reachable=False（不抛错）。
    engine = ScriptedEngine([_status(False)])
    runtime = _runtime(engine)
    await _run_cycles(runtime, interval=0.01, cycles=1)

    assert runtime.datasource_status["reachable"] is False
    assert runtime.datasource_status["summary"]["failed_tables"] == 1


@pytest.mark.asyncio
async def test_watchdog_tracks_flip_to_stale():
    # 健康 -> 在线但空表(stale)：看门狗持续写最新状态。
    engine = ScriptedEngine([_status(True, stale=False), _status(True, stale=True)])
    runtime = _runtime(engine)
    await _run_cycles(
        runtime, interval=0.01, cycles=2,
        expect={"reachable": True, "stale": True},
    )

    assert engine.probe_calls >= 2
    assert runtime.datasource_status["reachable"] is True
    assert runtime.datasource_status["stale"] is True


@pytest.mark.asyncio
async def test_watchdog_tracks_recovery():
    # 不可达 -> 恢复：最终状态为可达且不陈旧。
    engine = ScriptedEngine([_status(False), _status(True, stale=False)])
    runtime = _runtime(engine)
    await _run_cycles(
        runtime, interval=0.01, cycles=2,
        expect={"reachable": True, "stale": False},
    )

    assert runtime.datasource_status["reachable"] is True
    assert runtime.datasource_status["stale"] is False


@pytest.mark.asyncio
async def test_watchdog_disabled_when_interval_nonpositive():
    engine = ScriptedEngine([_status(True)])
    runtime = _runtime(engine)
    # interval<=0：协程立即返回，不做任何 probe。
    await datasource_watchdog(runtime, interval=0)
    assert engine.probe_calls == 0

    engine.probe_calls = 0
    await datasource_watchdog(runtime, interval=-5)
    assert engine.probe_calls == 0


@pytest.mark.asyncio
async def test_watchdog_default_interval_from_settings():
    # 不显式传 interval：从 settings.effective_starrocks.probe_interval 读取（=0 关闭）。
    engine = ScriptedEngine([_status(True)])
    runtime = _runtime(engine, interval=0)  # settings 里 probe_interval=0
    await datasource_watchdog(runtime)
    assert engine.probe_calls == 0


@pytest.mark.asyncio
async def test_watchdog_handles_missing_engine():
    # runtime 没有 metric_engine：不抛错，写不可达结构化状态。
    settings = SimpleNamespace()
    settings.effective_starrocks = SimpleNamespace(probe_interval=60)
    runtime = SimpleNamespace(settings=settings, datasource_status={})
    task = asyncio.create_task(datasource_watchdog(runtime, interval=0.01))
    while not runtime.datasource_status:
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert runtime.datasource_status["reachable"] is False
