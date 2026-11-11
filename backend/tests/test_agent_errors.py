"""P0-5 报错分析处理（FR6）单元测试：错误分类 + 限次自愈。

覆盖：
- classify_exception 对各类原始异常的收敛（解析/编译/权限/数据源/执行）
- HarnessViolationError 的权限/超时映射
- AgentError 结构化字段（to_dict）
- recover_parse：LLM 失败 -> 规则兜底自愈；兜底也失败 -> 抛 ParseError 并回写样本
- recover_datasource：瞬时失败 -> 退避重查自愈；耗尽 -> 返回 DataSourceError
- make_recovery_hook：only_recovered 避免与服务层重复采样
"""

from __future__ import annotations

import pytest

from dmf_agent.agents.metric_agent.errors import (
    AuthorizationError,
    DataSourceError,
    ErrorCategory,
    ExecutionError,
    ParseError,
    build_error_sample,
    classify_exception,
)
from dmf_agent.agents.metric_agent.recovery import (
    RecoverySettings,
    make_recovery_hook,
    recover_datasource,
    recover_parse,
)
from dmf_agent.harness import HarnessViolationError


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------


def test_classify_known_agent_error_keeps_node():
    err = ParseError("x", node="extract_elements")
    out = classify_exception(err, node="other")
    assert out is err
    assert out.node == "extract_elements"


def test_classify_value_error_is_parse():
    err = classify_exception(ValueError("bad json"))
    assert isinstance(err, ParseError)
    assert err.retryable is True
    assert err.fix_hint == "reparse_rule"


def test_classify_json_decode_is_parse():
    import json

    err = classify_exception(json.JSONDecodeError("e", "doc", 0))
    assert isinstance(err, ParseError)


def test_classify_sqlalchemy_is_datasource():
    # 用类名特征模拟 sqlalchemy 连接错误（不依赖驱动是否安装）
    class FakeOperationalError(Exception):
        pass

    FakeOperationalError.__name__ = "OperationalError"
    FakeOperationalError.__module__ = "sqlalchemy.exc"
    err = classify_exception(FakeOperationalError("conn reset"))
    assert isinstance(err, DataSourceError)
    assert err.retryable is True
    assert err.fix_hint == "retry_query"


def test_classify_generic_is_execution():
    err = classify_exception(RuntimeError("boom"))
    assert isinstance(err, ExecutionError)
    assert err.retryable is False


def test_classify_harness_permission():
    hv = HarnessViolationError("HARNESS_PERMISSION_DENIED", "需要权限", {"missing": ["x"]})
    err = classify_exception(hv)
    assert isinstance(err, AuthorizationError)
    assert int(err.category) == 3


def test_classify_harness_timeout():
    hv = HarnessViolationError("HARNESS_TIMEOUT", "超时")
    err = classify_exception(hv)
    assert isinstance(err, ExecutionError)
    assert int(err.category) == 5


def test_agent_error_to_dict():
    err = DataSourceError("查询失败", code="E4002", node="execute_query")
    d = err.to_dict()
    assert d["code"] == "E4002"
    assert d["category"] == int(ErrorCategory.DATASOURCE)
    assert d["category_label"] == "数据源"
    assert d["retryable"] is True
    assert str(err) == "[E4002] 查询失败"


def test_build_error_sample():
    err = ParseError("失败", code="E1001", node="extract_elements")
    sample = build_error_sample(
        err, recovered=True, attempts=2,
        ctx={"session_id": "s1", "user_id": "u1", "tenant_id": "t1", "round_id": "r1", "node": "extract_elements", "trace_snippet": "hi"},
    )
    assert sample["error_code"] == "E1001"
    assert sample["category"] == 1
    assert sample["recovered"] is True
    assert sample["attempts"] == 2
    assert sample["session_id"] == "s1"


# --------------------------------------------------------------------------
# 限次自愈：解析类
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_parse_falls_back_to_rule():
    settings = RecoverySettings(max_retries=2, backoff_seconds=0)
    calls = {"primary": 0, "fallback": 0}

    async def primary():
        calls["primary"] += 1
        raise ValueError("llm broken")

    async def fallback():
        calls["fallback"] += 1
        return {"query_elements": {"metrics": []}, "mock_mode": True}

    recorded = []
    hook = lambda e, r, a: recorded.append((e, r, a))  # noqa: E731

    out = await recover_parse("extract_elements", primary, fallback, settings, on_recovery=hook)
    assert out["mock_mode"] is True
    assert calls["primary"] == 1 and calls["fallback"] == 1
    # 自愈成功 -> 回写 recovered=True
    assert len(recorded) == 1 and recorded[0][1] is True


@pytest.mark.asyncio
async def test_recover_parse_exhaustion_raises_parse_error():
    settings = RecoverySettings(max_retries=2, backoff_seconds=0)
    recorded = []

    async def primary():
        raise ValueError("llm broken")

    async def fallback():
        raise RuntimeError("rule also broken")

    hook = lambda e, r, a: recorded.append((e, r, a))  # noqa: E731

    with pytest.raises(ParseError):
        await recover_parse("extract_elements", primary, fallback, settings, on_recovery=hook)
    # 兜底也失败 -> 回写 recovered=False（服务层会再统一采样，节点侧不重复）
    assert len(recorded) == 1 and recorded[0][1] is False


# --------------------------------------------------------------------------
# 限次自愈：数据源
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_datasource_retries_then_succeeds():
    settings = RecoverySettings(datasource_max_retries=2, backoff_seconds=0)
    attempts = {"n": 0}

    async def query_fn():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DataSourceError("conn reset")
        return {"columns": ["a"], "rows": [[1]]}

    result, err, n = await recover_datasource(query_fn, settings, node="execute_query")
    assert err is None
    assert result is not None
    assert n == 2  # 重试一次后成功


@pytest.mark.asyncio
async def test_recover_datasource_exhaustion_returns_error():
    settings = RecoverySettings(datasource_max_retries=2, backoff_seconds=0)
    attempts = {"n": 0}

    async def query_fn():
        attempts["n"] += 1
        raise DataSourceError("down")

    result, err, n = await recover_datasource(query_fn, settings, node="execute_query")
    assert result is None
    assert isinstance(err, DataSourceError)
    assert n == 3  # 首次 + 2 次重试


@pytest.mark.asyncio
async def test_recover_datasource_non_retryable_not_retried():
    settings = RecoverySettings(datasource_max_retries=2, backoff_seconds=0)
    attempts = {"n": 0}

    async def query_fn():
        attempts["n"] += 1
        raise AuthorizationError("no permission")

    result, err, n = await recover_datasource(query_fn, settings, node="execute_query")
    assert result is None
    assert isinstance(err, AuthorizationError)
    assert n == 1  # 不可重试，立即返回


# --------------------------------------------------------------------------
# 采样钩子：避免重复计数
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_hook_only_recovered():
    repo = _FakeRepo()
    ctx = {"session_id": "s1"}
    hook = make_recovery_hook(repo, ctx, only_recovered=True)
    err = ParseError("x", node="extract_elements")
    await hook(err, True, 2)   # 应记录
    await hook(err, False, 2)  # 不应记录（由服务层统一采样）
    assert len(repo.samples) == 1
    assert repo.samples[0]["recovered"] is True


class _FakeRepo:
    def __init__(self) -> None:
        self.samples: list = []

    async def add(self, sample):
        self.samples.append(sample)
        return len(self.samples)
