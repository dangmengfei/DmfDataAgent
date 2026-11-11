"""P0-8 数据源连通性探针测试（semantic/probe.py）。

用内存假 executor 模拟数据源，按 SQL 关键字返回不同结果（版本/COUNT/MAX/故障），
验证探针在"全部正常 / 表空 / 数据陈旧 / 某表探测失败 / 不可达 / 整体超时"各情形下
都输出结构化结果且不抛错。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from dmf_agent.semantic.probe import probe_datasource


def _recent_date(days_ago: int = 1) -> str:
    """相对今天的日期：陈旧判定按「距今天数」算，用例不能写死日期，否则会随时间失效。"""
    return (date.today() - timedelta(days=days_ago)).isoformat()


class FakeExecutor:
    """按 SQL 内容脚本化返回的假数据源执行器。"""

    def __init__(
        self,
        *,
        reachable: bool = True,
        scripts: Dict[str, List[List[Any]]] | None = None,
        fail_sql: str = "",
        ping_reachable: bool | None = None,
    ) -> None:
        self.reachable = reachable
        self.scripts = scripts or {}
        self.fail_sql = fail_sql
        self._ping_reachable = ping_reachable
        self.calls: List[str] = []

    async def ping(self) -> Dict[str, Any]:
        if self._ping_reachable is not None:
            return {
                "reachable": self._ping_reachable,
                "mode": "starrocks",
                "detail": "stub:19030",
                "elapsed_ms": 1.2,
            }
        return {
            "reachable": self.reachable,
            "mode": "starrocks" if self.reachable else "disabled",
            "detail": "stub:19030" if self.reachable else "未配置",
            "elapsed_ms": 1.2,
        }

    async def fetch(self, sql: str) -> Dict[str, Any]:
        self.calls.append(sql)
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError(f"forced failure: {sql[:40]}")
        for key, rows in self.scripts.items():
            if key in sql:
                return {
                    "columns": ["c"],
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": False,
                    "elapsed_ms": 0.5,
                }
        return {
            "columns": ["c"],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "elapsed_ms": 0.5,
        }


def _model(name: str, table: str, time_dim: str | None = None):
    td = (
        SimpleNamespace(name=time_dim, expr=time_dim)
        if time_dim is not None
        else None
    )
    m = SimpleNamespace(name=name, table=table)
    m.table_ref = table
    m.time_dimension = lambda: td
    return m


def _engine(executor: FakeExecutor, models: List[Any]):
    manifest = SimpleNamespace(models=models)
    layer = SimpleNamespace(manifest=manifest)
    return SimpleNamespace(executor=executor, layer=layer)


@pytest.mark.asyncio
async def test_probe_all_healthy():
    fresh = _recent_date()
    executor = FakeExecutor(
        scripts={
            "VERSION()": [[1, "8.0.0", "dw"]],
            "COUNT(*)": [[42]],
            "MAX(": [[fresh]],
        }
    )
    engine = _engine(executor, [_model("sales", "dw.fact_sales", "dt")])
    out = await probe_datasource(engine)

    assert out["reachable"] is True
    assert out["version"] == "8.0.0"
    assert out["db"] == "dw"
    assert out["latency_ms"] is not None
    assert out["data_as_of"] == fresh
    assert out["stale"] is False
    table = out["tables"][0]
    assert table["row_count"] == 42
    assert table["latest_at"] == fresh
    assert table["ok"] is True
    assert table["stale"] is False
    assert out["summary"] == {
        "total": 1,
        "reachable_tables": 1,
        "empty_tables": 0,
        "stale_tables": 0,
        "failed_tables": 0,
    }


@pytest.mark.asyncio
async def test_probe_empty_table_flagged():
    executor = FakeExecutor(
        scripts={
            "VERSION()": [[1, "8.0.0", "dw"]],
            "COUNT(*)": [[0]],
            "MAX(": [[None]],
        }
    )
    engine = _engine(executor, [_model("empty_model", "dw.fact_empty", "dt")])
    out = await probe_datasource(engine)

    assert out["stale"] is True
    assert out["summary"]["empty_tables"] == 1
    assert out["tables"][0]["row_count"] == 0


@pytest.mark.asyncio
async def test_probe_stale_data_flagged():
    executor = FakeExecutor(
        scripts={
            "VERSION()": [[1, "8.0.0", "dw"]],
            "COUNT(*)": [[10]],
            "MAX(": [["2020-01-01"]],
        }
    )
    engine = _engine(executor, [_model("old_model", "dw.fact_old", "dt")])
    out = await probe_datasource(engine, stale_days=3)

    assert out["tables"][0]["stale"] is True
    assert out["summary"]["stale_tables"] == 1
    assert out["stale"] is True


@pytest.mark.asyncio
async def test_probe_table_failure_isolated():
    # COUNT 在坏表上抛错，好表正常；探针不抛出，坏表单独标注。
    executor = FakeExecutor(
        scripts={
            "VERSION()": [[1, "8.0.0", "dw"]],
        },
        fail_sql="dw.fact_broken",
    )

    async def fetch_dispatch(sql: str):
        if "COUNT(*)" in sql and "fact_broken" not in sql:
            return {
                "columns": ["c"],
                "rows": [[5]],
                "row_count": 1,
                "truncated": False,
                "elapsed_ms": 0.5,
            }
        if "MAX(" in sql and "fact_broken" not in sql:
            return {
                "columns": ["c"],
                "rows": ["2026-09-19"],
                "row_count": 1,
                "truncated": False,
                "elapsed_ms": 0.5,
            }
        return await FakeExecutor.fetch(executor, sql)

    executor.fetch = fetch_dispatch  # type: ignore

    engine = _engine(
        executor,
        [
            _model("good", "dw.fact_good", "dt"),
            _model("broken", "dw.fact_broken", "dt"),
        ],
    )
    out = await probe_datasource(engine)

    assert out["summary"]["failed_tables"] == 1
    assert out["summary"]["reachable_tables"] == 1
    broken = next(t for t in out["tables"] if t["model"] == "broken")
    assert broken["ok"] is False
    assert broken["error"]
    good = next(t for t in out["tables"] if t["model"] == "good")
    assert good["ok"] is True
    assert good["row_count"] == 5


@pytest.mark.asyncio
async def test_probe_unreachable_short_circuits():
    executor = FakeExecutor(ping_reachable=False)
    engine = _engine(executor, [_model("m", "dw.t", "dt")])
    out = await probe_datasource(engine)

    assert out["reachable"] is False
    assert out["tables"] == []
    assert out["summary"]["total"] == 0
    # 不可达时不应去探任何表
    assert executor.calls == []


@pytest.mark.asyncio
async def test_probe_total_timeout_returns_structured():
    # executor.ping 一直挂着 -> 触发整体超时，探针仍返回结构化结果而非抛出。
    class HangingExecutor(FakeExecutor):
        async def ping(self):  # noqa: D401
            await asyncio.sleep(5)
            return {"reachable": True}

    engine = _engine(HangingExecutor(), [_model("m", "dw.t", "dt")])
    out = await probe_datasource(engine, total_timeout=0.2)

    assert out["reachable"] is False
    assert "超时" in out["detail"]
    assert out["stale"] is True


def test_probe_endpoint_wiring():
    """GET /semantic/health 返回探针结果并缓存到 runtime.datasource_status。"""
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        rt = app.state.runtime
        # 无可达库（本机测试环境）时探针 reachable=False，但端点应 200 且结构完整。
        resp = client.get("/api/v1/semantic/health")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "reachable",
            "mode",
            "latency_ms",
            "tables",
            "summary",
            "checked_at",
            "stale",
        ):
            assert key in body
        # 结果已缓存到 runtime
        assert rt.datasource_status.get("checked_at") == body["checked_at"]
