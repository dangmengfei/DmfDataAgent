"""指标平台（HM）取数适配测试：DSL -> 请求体、响应解析、重试。

⚠️ 本文件覆盖的是 `dmf_agent.integrations.metric_platform`（离线元信息 / 历史取数
适配层），**已不在主流程链路上**。主流程取数为 语义层 -> MetricFlow 编译 -> StarRocks，
见 tests/test_semantic.py、tests/test_graph.py（假语义引擎）。
"""

import httpx
import pytest

from dmf_agent.config.settings import HMSettings, Settings
from dmf_agent.integrations.metric_platform import gain_api as hm_api
from dmf_agent.integrations.metric_platform.gain_api import (
    HmDataError,
    HmGainClient,
    build_gain_request,
    fetch_metric_data,
    parse_gain_response,
)
from fakes import FakeMetricEngine, make_deps

HM_SETTINGS = Settings(hm=HMSettings(api_base="http://hm:8080"))

DSL = {
    "metrics": [{"code": "67", "name": "国内日销量"}],
    "dimensions": ["城市"],
    "filters": [{"dimension": "车型", "dimension_value": ["枭龙MAX"], "filter_operator": "in"}],
    "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
    "order_by": None,
    "period_comparison": False,
    "period_comparison_type": None,
}
DIM_NAME2ID = {"城市": 38, "车型": 12}


# ---- build_gain_request（DSL -> HM 请求体） ----


def test_build_request_metrics_and_dims():
    body = build_gain_request(DSL, DIM_NAME2ID, HM_SETTINGS)
    assert body["chosenMetricsIds"] == [67]
    assert body["chosenDimensionIds"] == [38]
    assert body["timePeriodCondition"] == {
        "statTime": "day",
        "beginTime": "2026-08-01",
        "endTime": "2026-08-07",
    }
    assert body["pageSize"] == 200000
    assert body["pageNum"] == 1


def test_build_request_condition_member():
    body = build_gain_request(DSL, DIM_NAME2ID, HM_SETTINGS)
    assert body["condition"] == {
        "son": [],
        "opType": "AND",
        "member": [{"id": 12, "keys": ["枭龙MAX"], "operator": "in"}],
    }


def test_build_request_default_order_bys():
    body = build_gain_request(DSL, DIM_NAME2ID, HM_SETTINGS)
    assert body["orderBys"] == [
        {"subjectType": "dt", "order": "DESC"},
        {"subjectType": "metrics", "subjectKey": 67, "order": "DESC"},
    ]


def test_build_request_no_dim_ids_skips_condition():
    body = build_gain_request(DSL, {}, HM_SETTINGS)
    assert "condition" not in body
    assert body["chosenDimensionIds"] == []


def test_build_request_metric_code_invalid_skipped():
    dsl = {**DSL, "metrics": [{"code": "not-an-id", "name": "未知"}]}
    body = build_gain_request(dsl, DIM_NAME2ID, HM_SETTINGS)
    assert body["chosenMetricsIds"] == []


def test_build_request_reversed_time_swapped():
    dsl = {**DSL, "time_range": {"start": "2026-08-07", "end": "2026-08-01", "granularity": "day"}}
    body = build_gain_request(dsl, DIM_NAME2ID, HM_SETTINGS)
    assert (body["timePeriodCondition"]["beginTime"], body["timePeriodCondition"]["endTime"]) == (
        "2026-08-01",
        "2026-08-07",
    )


def test_build_request_pop_builds_precomputation_infos():
    dsl = {**DSL, "period_comparison": True, "period_comparison_type": "yoy"}
    body = build_gain_request(dsl, DIM_NAME2ID, HM_SETTINGS)
    assert body["precomputationInfos"] == [
        {
            "metricsId": 67,
            "operators": "growthRate,growthValue,baseValue",
            "lagOffsetType": "yoy",
        }
    ]


def test_build_request_pop_no_metrics_skipped():
    dsl = {**DSL, "metrics": [], "period_comparison": True, "period_comparison_type": "yoy"}
    body = build_gain_request(dsl, DIM_NAME2ID, HM_SETTINGS)
    assert "precomputationInfos" not in body


def test_build_request_pop_unsupported_type_falls_back_pop():
    # year 粒度不支持 yoy -> 回退 pop（老 STATTIME_2_POP 逻辑）
    dsl = {
        **DSL,
        "time_range": {"start": "2026-01-01", "end": "2026-12-31", "granularity": "year"},
        "period_comparison": True,
        "period_comparison_type": "yoy",
    }
    body = build_gain_request(dsl, DIM_NAME2ID, HM_SETTINGS)
    assert body["precomputationInfos"][0]["lagOffsetType"] == "pop"


# ---- normalize_filter（build_dsl 统一过滤格式 / 语义层共用） ----


def test_normalize_llm_filter_to_kv():
    from dmf_agent.semantic.query import normalize_filter

    assert normalize_filter({"field": "门店", "operator": "in", "values": ["直营"]}) == {
        "dimension": "门店",
        "dimension_value": ["直营"],
        "filter_operator": "in",
    }


def test_normalize_mock_filter_passthrough():
    from dmf_agent.semantic.query import normalize_filter

    raw = {"dimension": "地区", "dimension_value": ["华东"], "filter_operator": "in"}
    assert normalize_filter(raw) == raw


def test_normalize_operator_alias():
    from dmf_agent.semantic.query import normalize_filter

    assert normalize_filter({"field": "x", "operator": "not_in", "values": ["a"]})["filter_operator"] == "not in"
    assert normalize_filter({"field": "x", "operator": "==", "values": ["a"]})["filter_operator"] == "="
    assert normalize_filter({"field": "x"})["filter_operator"] == "in"


def test_normalize_missing_values_empty():
    from dmf_agent.semantic.query import normalize_filter

    assert normalize_filter({"field": "门店", "operator": "="})["dimension_value"] == []


# ---- parse_gain_response（HM 响应 -> columns/rows） ----


def test_parse_gain_response_empty():
    assert parse_gain_response({}) == {
        "columns": [],
        "rows": [],
        "total": 0,
        "sql": "",
        "msg": "",
    }


def test_parse_gain_response_columns_rows():
    data = {
        "list": [
            {"统计时间": "2026-08-01", "GMV": 100},
            {"统计时间": "2026-08-02", "GMV": "--"},
        ],
        "schema": {"统计时间": "统计时间", "GMV": "GMV"},
        "total": 2,
        "sql": "select * from t",
    }
    result = parse_gain_response(data)
    assert result["columns"] == ["统计时间", "GMV"]
    assert result["rows"][0] == ["2026-08-01", 100]
    # "--" 空值占位 -> None（数值保持 HM 原始 JSON 类型）
    assert result["rows"][1] == ["2026-08-02", None]
    assert result["total"] == 2
    assert result["sql"] == "select * from t"


def test_parse_gain_response_schema_drops_absent_keys():
    data = {
        "list": [{"统计时间": "2026-08-01", "GMV": 1, "EXTRA": "x"}],
        "schema": {"统计时间": "统计时间", "GMV": "GMV"},
        "total": 1,
    }
    result = parse_gain_response(data)
    assert result["columns"] == ["统计时间", "GMV"]
    assert result["rows"] == [["2026-08-01", 1]]


# ---- fetch_metric_data（mock / 真实分支） ----


async def test_fetch_mock_when_unconfigured():
    result = await fetch_metric_data(DSL, Settings())
    assert result["mock"] is True
    assert result["columns"][0] == "date"


class _FakeGainClient:
    def __init__(self, data):
        self.data = data
        self.body = None

    async def _post(self, body):
        self.body = body
        return self.data


async def test_fetch_real_uses_client_and_parses():
    fake = _FakeGainClient(
        {"list": [{"统计时间": "2026-08-01", "GMV": 100}], "schema": {"统计时间": "统计时间", "GMV": "GMV"}, "total": 1}
    )
    result = await fetch_metric_data(DSL, HM_SETTINGS, dim_name2id=DIM_NAME2ID, client=fake)
    assert result["mock"] is False
    assert result["columns"] == ["统计时间", "GMV"]
    assert result["rows"] == [["2026-08-01", 100]]
    # 请求体由 build_gain_request 组装并透出，便于观测
    assert fake.body["chosenMetricsIds"] == [67]


# ---- HmGainClient._post（重试 / 异常码） ----


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeAsyncClient:
    """httpx.AsyncClient 替换：依次消费 [异常或响应]，用于重试测试。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        item = self._results[0]
        if len(self._results) > 1:
            self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


async def _noop_sleep(s):
    return None


async def test_post_retries_then_succeeds(monkeypatch):
    fake = _FakeAsyncClient([httpx.ConnectError("boom"), {"code": 200, "data": {"list": []}}])
    monkeypatch.setattr(hm_api.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(hm_api.asyncio, "sleep", _noop_sleep)
    data = await HmGainClient(HM_SETTINGS)._post({"x": 1})
    assert data == {"list": []}
    assert fake.calls == 2


async def test_post_retries_exhausted_raises(monkeypatch):
    settings = Settings(hm=HMSettings(api_base="http://hm:8080", retry_times=1))
    fake = _FakeAsyncClient([httpx.ConnectError("boom"), httpx.ConnectError("boom2")])
    monkeypatch.setattr(hm_api.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setattr(hm_api.asyncio, "sleep", _noop_sleep)
    with pytest.raises(HmDataError):
        await HmGainClient(settings)._post({"x": 1})


async def test_post_non_200_raises(monkeypatch):
    monkeypatch.setattr(
        hm_api.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient([{"code": 500, "msg": "internal error"}])
    )
    with pytest.raises(HmDataError, match="500"):
        await HmGainClient(HM_SETTINGS)._post({"x": 1})


# ---- execute_query 节点（语义引擎取数；失败以 error 结果返回，不造假） ----


class _FakeDimClient:
    configured = True

    async def fetch_dimensions(self):
        return [{"cnName": "门店", "id": 5}]


async def test_load_dim_name2id():
    """指标平台维度名 -> 编码映射（integrations 内部使用）。"""
    from dmf_agent.integrations.metric_platform.gain_api import load_dim_name2id

    assert await load_dim_name2id(_FakeDimClient()) == {"门店": 5}


async def test_execute_query_engine_error_returns_error():
    """语义引擎取数失败（如 StarRocks 未配置）-> query_result 带 error，mock 恒为 False。"""
    from dmf_agent.agents.metric_agent.nodes.execute_query import build_execute_query_node

    deps = make_deps(engine=FakeMetricEngine(fail_with="StarRocks 未配置（请设置 DMF_STARROCKS-HOST）"))
    node = build_execute_query_node(deps)
    result = await node({"dsl": {"metrics": [{"name": "GMV"}]}})
    assert result["query_result"]["mock"] is False
    assert result["query_result"]["engine"] == "metricflow"
    assert "StarRocks 未配置" in result["query_result"]["error"]
    assert result["query_result"]["rows"] == []


async def test_execute_query_without_engine_returns_error():
    """语义引擎未装配 -> 明确报错，不静默降级。"""
    from dmf_agent.agents.metric_agent.nodes.execute_query import build_execute_query_node

    node = build_execute_query_node(make_deps(metric_engine=None))
    result = await node({"dsl": {"metrics": [{"name": "GMV"}]}})
    assert result["query_result"]["mock"] is False
    assert "语义引擎未装配" in result["query_result"]["error"]


async def test_execute_query_success_passthrough():
    """取数成功 -> 原样透传语义引擎结果（含 SQL / 行数 / 耗时）。"""
    from dmf_agent.agents.metric_agent.nodes.execute_query import build_execute_query_node

    node = build_execute_query_node(make_deps())
    result = await node({"dsl": {"metrics": [{"name": "GMV"}], "dimensions": []}})
    qr = result["query_result"]
    assert qr["mock"] is False
    assert qr["columns"] == ["统计日期", "GMV"]
    assert qr["row_count"] == 3
    assert "fake metricflow sql" in qr["sql"]


# ---- extract_elements mock 同环比类型推断 ----


def test_infer_pop_type():
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _infer_pop_type

    assert _infer_pop_type("看周同比") == "wow"
    assert _infer_pop_type("看季同比") == "qoq"
    assert _infer_pop_type("看同比") == "yoy"
    assert _infer_pop_type("看环比") == "mom"
    assert _infer_pop_type("看GMV") is None
