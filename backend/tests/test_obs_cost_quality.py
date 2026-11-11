"""FR11 可观测补齐：LLM 成本计价 / 预算告警 / 质量与采纳率看板。

覆盖 `observability/cost.py`、`observability/quality.py`、
`trace_records` 成本与归属列（迁移 0021）以及 `GET /obs/cost`、`GET /obs/quality`。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dmf_agent.config.settings import CostSettings, Settings
from dmf_agent.db.memory import (
    InMemoryBadCaseRepository,
    InMemoryFewShotRepository,
    InMemoryTraceRepository,
)
from dmf_agent.observability.cost import budget_alerts, estimate_cost, resolve_price
from dmf_agent.observability.quality import aggregate_quality, round_signals
from dmf_agent.schemas.feedback import BadCaseData, FewShotData
from dmf_agent.schemas.trace import TraceData


def _settings(**kw) -> Settings:
    s = Settings()
    s.cost = CostSettings(**kw)
    return s


def _record(**kw) -> TraceData:
    base = dict(session_id="s", round_id="r1")
    base.update(kw)
    return TraceData(**base)


# ---- 单价解析 ----


def test_resolve_price_exact_and_prefix():
    table = {"deepseek-chat": {"input": 2.0, "output": 8.0}, "gpt": {"input": 10.0, "output": 30.0}}
    assert resolve_price("deepseek-chat", table) == (2.0, 8.0, "exact")
    # 带版本后缀走最长前缀匹配
    assert resolve_price("deepseek-chat-0506", table) == (2.0, 8.0, "prefix:deepseek-chat")
    assert resolve_price("gpt-4o", table)[2] == "prefix:gpt"
    # 未命中
    assert resolve_price("no-such-model", table) == (0.0, 0.0, "default")
    assert resolve_price("", table) == (0.0, 0.0, "default")


def test_resolve_price_bad_table_value():
    # 单价表被写成非法结构时降级为 0，不抛错（配置错误不该拖垮主链路）
    assert resolve_price("m", {"m": {"input": "x", "output": None}}) == (0.0, 0.0, "exact")


# ---- 成本估算 ----


def test_estimate_cost_known_model():
    s = _settings()
    # deepseek-chat：输入 2 元/百万、输出 8 元/百万
    info = estimate_cost("deepseek-chat", 1000, 500, settings=s)
    assert info["cost"] == 0.006
    assert info["currency"] == "CNY"
    assert info["price_source"] == "exact"


def test_estimate_cost_unknown_model_uses_default():
    s = _settings(default_input_price=1.0, default_output_price=4.0)
    info = estimate_cost("my-model", 1_000_000, 1_000_000, settings=s)
    assert info["price_source"] == "default"
    assert info["cost"] == 5.0


def test_estimate_cost_zero_cases():
    s = _settings()
    assert estimate_cost("deepseek-chat", None, None, settings=s)["cost"] == 0.0
    assert estimate_cost("deepseek-chat", 0, 0, settings=s)["cost"] == 0.0
    # 负数防御
    assert estimate_cost("deepseek-chat", -100, -100, settings=s)["cost"] == 0.0
    # 未启用计价
    off = _settings(enabled=False)
    assert estimate_cost("deepseek-chat", 1000, 500, settings=off)["cost"] == 0.0
    assert estimate_cost("deepseek-chat", 1000, 500, settings=off)["pricing_enabled"] is False


def test_estimate_cost_without_settings():
    # 未装配 settings（如单元测试直接调用）不报错，走默认配置
    info = estimate_cost("deepseek-chat", 1000, 500)
    assert info["cost"] > 0


# ---- 预算告警 ----


def test_budget_alerts_warn_and_exceeded():
    s = _settings(daily_budget=10.0, monthly_budget=100.0, alert_ratio=0.8)
    assert budget_alerts(daily_cost=1.0, monthly_cost=1.0, settings=s) == []
    warn = budget_alerts(daily_cost=8.5, monthly_cost=90.0, settings=s)
    assert len(warn) == 2
    assert all(a["level"] == "warn" for a in warn)
    assert warn[0]["period"] == "daily"
    over = budget_alerts(daily_cost=12.0, monthly_cost=1.0, settings=s)
    assert len(over) == 1 and over[0]["level"] == "exceeded"
    assert over[0]["ratio"] == 1.2


def test_budget_alerts_unlimited():
    s = _settings(daily_budget=0.0, monthly_budget=0.0)
    assert budget_alerts(daily_cost=9999.0, monthly_cost=9999.0, settings=s) == []


# ---- 成本归集（内存仓储，与 SQL 同口径） ----


async def test_cost_breakdown_by_model_user_day():
    repo = InMemoryTraceRepository()
    await repo.add(
        _record(round_id="r1", model="deepseek-chat", user_id="u1", tenant_id="t1",
                prompt_tokens=1000, completion_tokens=500, total_tokens=1500, cost=0.006)
    )
    await repo.add(
        _record(round_id="r2", model="deepseek-chat", user_id="u2", tenant_id="t1",
                prompt_tokens=2000, completion_tokens=1000, total_tokens=3000, cost=0.012)
    )
    await repo.add(
        _record(round_id="r3", model="qwen-plus", user_id="u1", tenant_id="t2",
                prompt_tokens=100, completion_tokens=100, total_tokens=200, cost=0.001)
    )

    by_model = await repo.cost_breakdown(group_by="model")
    assert by_model[0]["key"] == "deepseek-chat"
    assert by_model[0]["rounds"] == 2
    assert abs(by_model[0]["cost"] - 0.018) < 1e-9
    assert by_model[0]["total_tokens"] == 4500

    # 按 cost 降序：u2(0.012) > u1(0.006+0.001)
    by_user = await repo.cost_breakdown(group_by="user")
    assert [i["key"] for i in by_user] == ["u2", "u1"]
    assert by_user[1]["rounds"] == 2

    by_day = await repo.cost_breakdown(group_by="day")
    assert len(by_day) == 1 and len(by_day[0]["key"]) == 10  # YYYY-MM-DD

    # 未知分组回落 model，不抛错
    assert (await repo.cost_breakdown(group_by="???"))[0]["key"] == "deepseek-chat"


async def test_cost_breakdown_since_filter():
    repo = InMemoryTraceRepository()
    old = await repo.add(_record(round_id="old", model="m", cost=9.0))
    old.created_at = datetime.now() - timedelta(days=10)
    await repo.add(_record(round_id="new", model="m", cost=1.0))
    rows = await repo.cost_breakdown(
        group_by="model", since=datetime.now() - timedelta(days=3)
    )
    assert len(rows) == 1 and abs(rows[0]["cost"] - 1.0) < 1e-9


# ---- 质量信号 ----


def test_round_signals_grounded_query():
    r = _record(
        round_id="r1", intent="metric_query", mock_mode=False,
        trace={"dsl": {"metrics": ["gmv"]},
               "query_result": {"columns": ["d", "gmv"], "rows": [["2026-01-01", 100]]},
               "final_answer": "GMV 为 100"},
    )
    s = round_signals(r)
    assert s["grounded"] is True
    assert s["empty"] is False
    assert s["hallucination_risk"] is False


def test_round_signals_empty_and_error():
    empty = round_signals(
        _record(round_id="r2", intent="metric_query",
                trace={"dsl": {}, "query_result": {"columns": ["d"], "rows": []},
                       "final_answer": "没有数据"})
    )
    assert empty["empty"] is True and empty["grounded"] is False

    err = round_signals(
        _record(round_id="r3", intent="metric_query", has_error=True,
                trace={"query_result": {"error": "数据源不可用"}, "final_answer": "抱歉"})
    )
    assert err["error"] is True
    assert err["grounded"] is False
    # 出错却仍给了答案 -> 计入幻觉风险
    assert err["hallucination_risk"] is True


def test_round_signals_mock_is_hallucination_risk():
    s = round_signals(
        _record(round_id="r4", intent="metric_query", mock_mode=True,
                trace={"final_answer": "（mock）GMV 为 100"})
    )
    assert s["mock"] is True and s["hallucination_risk"] is True and s["grounded"] is False


def test_round_signals_non_query_intent():
    # help 类意图不要求取数，有答复即算有据
    s = round_signals(_record(round_id="r5", intent="help", trace={"final_answer": "我能帮你查指标"}))
    assert s["grounded"] is True
    assert s["empty"] is False


def test_round_signals_clarified():
    s = round_signals(
        _record(round_id="r6", intent="metric_query",
                trace={"confirmation": {"clarification": {"type": "metric"}}})
    )
    assert s["clarified"] is True


def test_aggregate_quality_rates():
    records = [
        _record(round_id="r1", intent="metric_query",
                trace={"dsl": {}, "query_result": {"columns": ["a"], "rows": [[1]]},
                       "final_answer": "ok"}),
        _record(round_id="r2", intent="metric_query", mock_mode=True,
                trace={"final_answer": "mock"}),
        _record(round_id="r3", intent="help", trace={"final_answer": "hi"}),
        _record(round_id="r4", intent="metric_query",
                trace={"dsl": {}, "query_result": {"columns": ["a"], "rows": []},
                       "final_answer": "无数据"}),
    ]
    q = aggregate_quality(records, likes=3, dislikes=1)
    assert q["total"] == 4
    # 有据：r1（取到数）+ r3（help 有答复）= 2/4
    assert q["accuracy"]["grounded_rate"] == 50.0
    assert q["acceptance"] == {"likes": 3, "dislikes": 1, "feedback_total": 4, "rate": 75.0}
    assert q["mock_rate"] == 25.0
    assert q["empty_rate"] == 25.0
    assert q["hallucination_risk_rate"] == 25.0
    intents = {i["intent"]: i for i in q["by_intent"]}
    assert intents["metric_query"]["total"] == 3
    assert intents["help"]["grounded_rate"] == 100.0


def test_aggregate_quality_empty():
    q = aggregate_quality([])
    assert q["total"] == 0
    assert q["accuracy"]["grounded_rate"] == 0.0
    assert q["acceptance"]["rate"] == 0.0
    assert q["by_intent"] == []


# ---- 采纳率来源：点赞（few-shot source=like）与点踩（bad case）计数 ----


async def test_acceptance_counts_from_feedback_repos():
    few = InMemoryFewShotRepository()
    bad = InMemoryBadCaseRepository()
    await few.add(FewShotData(id=0, session_id="s", message_id="m1", question="q", answer="a",
                              intent="metric_query", category="intention", source="like", status="active"))
    await few.add(FewShotData(id=0, session_id="s", message_id="m2", question="q2", answer="a2",
                              intent="metric_query", category="intention", source="manual", status="active"))
    await bad.add(BadCaseData(id=0, session_id="s", message_id="m3", question="q3", answer="a3"))

    likes = await few.count(source="like")
    dislikes = await bad.count()
    assert (likes, dislikes) == (1, 1)
    q = aggregate_quality([], likes=likes, dislikes=dislikes)
    assert q["acceptance"]["rate"] == 50.0


# ---- 看板 API ----


def _fake_runtime(trace_repo, few_shot_repo, bad_case_repo, settings):
    """路由函数直调用的最小 runtime（只用到 db 与 settings）。"""
    return SimpleNamespace(
        db=SimpleNamespace(
            trace_repo=trace_repo,
            few_shot_repo=few_shot_repo,
            bad_case_repo=bad_case_repo,
        ),
        settings=settings,
    )


async def _seed_cost_repo():
    repo = InMemoryTraceRepository()
    now = datetime.now()
    for i, (model, user, cost) in enumerate(
        [("deepseek-chat", "u1", 0.006), ("deepseek-chat", "u2", 0.012), ("qwen-plus", "u1", 0.001)]
    ):
        rec = TraceData(
            session_id="s-cost", round_id=f"api-{i}", intent="metric_query",
            mock_mode=False, skills=[], elapsed_ms=100.0, has_error=False,
            prompt_tokens=1000, completion_tokens=500, total_tokens=1500,
            user_id=user, tenant_id="t1", model=model, cost=cost,
            trace={"dsl": {}, "query_result": {"columns": ["a"], "rows": [[1]]},
                   "final_answer": "ok"},
        )
        saved = await repo.add(rec)
        saved.created_at = now  # 内存仓储按 add 顺序落时间，这里统一到"今天"
    return repo


async def test_obs_cost_route_breaks_down_and_alerts():
    from dmf_agent.api.routers.obs import obs_cost

    repo = await _seed_cost_repo()
    # 日预算 0.01 -> 今日成本 0.019 已超预算，应产出 exceeded 告警
    settings = _settings(daily_budget=0.01)
    runtime = _fake_runtime(repo, InMemoryFewShotRepository(), InMemoryBadCaseRepository(), settings)

    body = await obs_cost(group_by="model", days=7, limit=50, runtime=runtime)
    assert body["currency"] == "CNY" and body["pricing_enabled"] is True
    assert body["totals"]["rounds"] == 3
    assert abs(body["totals"]["cost"] - 0.019) < 1e-9
    assert body["items"][0]["key"] == "deepseek-chat"
    alerts = body["budget"]["alerts"]
    assert alerts and alerts[0]["level"] == "exceeded"
    assert body["budget"]["daily"]["used"] > 0

    by_user = await obs_cost(group_by="user", days=0, limit=50, runtime=runtime)
    assert {i["key"] for i in by_user["items"]} == {"u1", "u2"}


async def test_obs_quality_route_with_feedback():
    from dmf_agent.api.routers.obs import obs_quality

    repo = await _seed_cost_repo()
    # 追加一轮 mock（降级）与一轮空结果，验证质量信号在看板内生效
    await repo.add(_record(round_id="api-mock", intent="metric_query", mock_mode=True,
                           trace={"final_answer": "（mock）GMV 为 100"}))
    await repo.add(_record(round_id="api-empty", intent="metric_query",
                           trace={"dsl": {}, "query_result": {"columns": ["a"], "rows": []},
                                  "final_answer": "无数据"}))
    few = InMemoryFewShotRepository()
    await few.add(FewShotData(id=0, session_id="s", message_id="m1", question="q", answer="a",
                              intent="metric_query", category="intention", source="like", status="active"))
    bad = InMemoryBadCaseRepository()
    await bad.add(BadCaseData(id=0, session_id="s", message_id="m2", question="q", answer="a"))

    runtime = _fake_runtime(repo, few, bad, _settings())
    body = await obs_quality(days=7, limit=2000, runtime=runtime)
    assert body["sample"] == 5
    # 5 轮中 3 轮有据（其余：mock 1、空结果 1）
    assert body["accuracy"]["grounded_rate"] == 60.0
    assert body["acceptance"]["rate"] == 50.0
    assert body["hallucination_risk_rate"] == 20.0
    assert body["by_intent"][0]["intent"] == "metric_query"


def test_obs_cost_quality_api_smoke():
    """路由确实挂载并可访问（新增端点必须真跑一次，防止 schema 缺失导致启动失败）。"""
    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/v1/obs/cost", params={"group_by": "model"})
        assert r.status_code == 200
        assert set(r.json()) >= {"currency", "totals", "items", "budget"}
        # 非法分组键 -> 422
        assert client.get("/api/v1/obs/cost", params={"group_by": "drop"}).status_code == 422

        r2 = client.get("/api/v1/obs/quality")
        assert r2.status_code == 200
        assert set(r2.json()) >= {"accuracy", "acceptance", "by_intent"}
