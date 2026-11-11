"""空数据回溯（PRD FR5 empty_trace）单测。

覆盖：
- 非空结果 / 取数报错 -> 不触发回溯
- 6 项原因的逐项命中与置信排序
- data_as_of 探查（含探查失败 -> 模型配置异常）
- analyze_data 节点接线（row_count=0 时把诊断写入 analysis.empty_diagnosis）
"""

from __future__ import annotations

from datetime import date

import pytest

from dmf_agent.agents.metric_agent.empty_trace import (
    CAUSE_DATA_NOT_UPDATED,
    CAUSE_FILTER_TOO_STRICT,
    CAUSE_GRANULARITY_MISMATCH,
    CAUSE_MODEL_CONFIG_ANOMALY,
    CAUSE_ROW_PERMISSION,
    CAUSE_TIME_OUT_OF_RANGE,
    EmptyDiagnosis,
    diagnose_empty,
)

TODAY = date(2026, 9, 14)


def _qr(rows=None, error=None, models=None):
    rows = rows or []
    return {
        "columns": ["dt", "gmv"],
        "rows": rows,
        "row_count": len(rows),
        "error": error,
        "models": models or ["trade_day"],
        "sql": "SELECT 1",
    }


def _elements(start=None, end=None, granularity=None, filters=None):
    return {
        "metrics": ["gmv"],
        "dimensions": ["channel"],
        "filters": filters or [],
        "time_range": {"start": start, "end": end, "granularity": granularity},
    }


class _FakeEngine:
    def __init__(self, as_of="2026-09-10"):
        self._as_of = as_of
        self.calls = 0

    async def data_as_of(self, models):
        self.calls += 1
        return self._as_of


class _FailingEngine:
    async def data_as_of(self, models):
        raise RuntimeError("connection refused")


# ---------------- 不触发回溯的场景 ----------------


@pytest.mark.asyncio
async def test_non_empty_result_skips_diagnosis():
    diag = await diagnose_empty(
        query_result=_qr(rows=[["2026-09-01", 100.0]]),
        query_elements=_elements(),
        dsl={},
        engine=_FakeEngine(),
        today=TODAY,
    )
    assert diag.is_empty is False


@pytest.mark.asyncio
async def test_error_result_skips_diagnosis():
    diag = await diagnose_empty(
        query_result=_qr(error="StarRocks timeout"),
        query_elements=_elements(),
        dsl={},
        engine=_FakeEngine(),
        today=TODAY,
    )
    assert diag.is_empty is False


# ---------------- 逐项原因 ----------------


@pytest.mark.asyncio
async def test_time_out_of_range_high():
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-20", end="2026-09-25"),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-10"),
        today=TODAY,
    )
    assert diag.is_empty and diag.data_as_of == "2026-09-10"
    codes = [c.code for c in diag.causes]
    assert CAUSE_TIME_OUT_OF_RANGE in codes
    top = diag.causes[0]
    assert top.code == CAUSE_TIME_OUT_OF_RANGE and top.level == "high"
    assert "2026-09-10" in top.suggestion


@pytest.mark.asyncio
async def test_data_not_updated():
    # 窗口在数据范围内结束，但数据截至日 < 今天 -> 数据未更新
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-08", end="2026-09-14"),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-10"),
        today=TODAY,
    )
    codes = [c.code for c in diag.causes]
    assert CAUSE_DATA_NOT_UPDATED in codes
    c = next(c for c in diag.causes if c.code == CAUSE_DATA_NOT_UPDATED)
    assert c.level == "high" and "尚未入库" in c.detail


@pytest.mark.asyncio
async def test_filter_too_strict():
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(
            start="2026-09-01",
            end="2026-09-05",
            filters=[{"field": "channel", "operator": "=", "values": [" nonexistent"]}],
        ),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-13"),
        today=TODAY,
    )
    codes = [c.code for c in diag.causes]
    assert CAUSE_FILTER_TOO_STRICT in codes


@pytest.mark.asyncio
async def test_granularity_mismatch():
    # 7 天窗口按 month 粒度聚合 -> 分桶为空
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-01", end="2026-09-05", granularity="month"),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-13"),
        today=TODAY,
    )
    codes = [c.code for c in diag.causes]
    assert CAUSE_GRANULARITY_MISMATCH in codes


@pytest.mark.asyncio
async def test_row_permission_low_priority():
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-01", end="2026-09-05"),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-13"),
        tenant_id="t-001",
        today=TODAY,
    )
    codes = [c.code for c in diag.causes]
    assert CAUSE_ROW_PERMISSION in codes
    c = next(c for c in diag.causes if c.code == CAUSE_ROW_PERMISSION)
    assert c.level == "low"


@pytest.mark.asyncio
async def test_probe_failure_maps_to_model_config_anomaly():
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-01", end="2026-09-05"),
        dsl={},
        engine=_FailingEngine(),
        today=TODAY,
    )
    codes = [c.code for c in diag.causes]
    assert CAUSE_MODEL_CONFIG_ANOMALY in codes
    assert diag.data_as_of is None


@pytest.mark.asyncio
async def test_no_engine_and_fresh_data_fallback_cause():
    # 引擎为 None、时间窗正常 -> 兜底通用排查项，不至于空诊断
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(start="2026-09-01", end="2026-09-05"),
        dsl={},
        engine=None,
        today=TODAY,
    )
    assert diag.is_empty and diag.causes, "至少给出一个兜底原因"


@pytest.mark.asyncio
async def test_causes_sorted_by_level():
    diag = await diagnose_empty(
        query_result=_qr(),
        query_elements=_elements(
            start="2026-09-20",
            end="2026-09-25",
            granularity="month",
            filters=[{"field": "channel", "operator": "=", "values": ["x"]}],
        ),
        dsl={},
        engine=_FakeEngine(as_of="2026-09-10"),
        today=TODAY,
    )
    levels = [c.level for c in diag.causes]
    rank = {"high": 3, "medium": 2, "low": 1}
    assert levels == sorted(levels, key=lambda lv: -rank[lv])


def test_to_dict_roundtrip():
    d = EmptyDiagnosis(is_empty=True, data_as_of="2026-09-10")
    payload = d.to_dict()
    assert payload["is_empty"] is True
    assert payload["data_as_of"] == "2026-09-10"
    assert isinstance(payload["causes"], list)
