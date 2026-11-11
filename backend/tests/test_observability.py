"""观测层测试：OTel span 采集/落库 + trace 落库聚合 + obs API + 导出。

覆盖"遵循 OpenTelemetry、入库、不接外部观测平台"的改造：
- span 采集（RoundTracer）产出的字段符合 OTel Span 语义（trace_id/span_id/parent/status）；
- span 落 trace_spans（内存实现等价），轮级汇总落 trace_records；
- obs API 兼容既有三个接口，并新增 rounds / spans / sessions / export；
- 已移除 agent-insight / OTLP 导出：配置里不再有 otlp_endpoint，health 用 otel_tracing。
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from dmf_agent.config.settings import ObservabilitySettings, Settings
from dmf_agent.db.memory import InMemorySpanRepository, InMemoryTraceRepository
from dmf_agent.observability import build_round_tracer
from dmf_agent.schemas.trace import (
    SpanData,
    TraceData,
    aggregate_spans,
    aggregate_traces,
)


def _record(**kw) -> TraceData:
    base = dict(session_id="s", round_id=f"r{kw.pop('i', 1)}")
    base.update(kw)
    return TraceData(**base)


def _span(**kw) -> SpanData:
    base = dict(
        trace_id="a" * 32,
        span_id="b" * 16,
        session_id="s",
        round_id="r1",
        name="node.execute_query",
        duration_ms=12.5,
    )
    base.update(kw)
    return SpanData(**base)


# ---- 内存 trace 仓储 + 聚合 ----


async def test_trace_repo_add_list_get():
    repo = InMemoryTraceRepository()
    await repo.add(_record(i=1, intent="metric_query"))
    await repo.add(_record(i=2, intent="diagnosis"))
    recent = await repo.list_recent(10)
    assert len(recent) == 2
    assert recent[0].round_id == "r2"  # 最近在前
    got = await repo.get("s", "r1")
    assert got is not None and got.intent == "metric_query"
    assert await repo.get("nope", "nope") is None


async def test_aggregate_traces():
    records = [
        _record(i=1, intent="metric_query", mock_mode=True, skills=["chart_visualization"], elapsed_ms=100),
        _record(i=2, intent="diagnosis", mock_mode=False, elapsed_ms=300),
    ]
    agg = aggregate_traces(records)
    assert agg["total"] == 2
    assert agg["mock_count"] == 1
    assert agg["avg_elapsed_ms"] == 200.0
    assert agg["intent_distribution"]["metric_query"] == 1
    assert agg["skill_distribution"]["chart_visualization"] == 1


async def test_aggregate_traces_extended_metrics():
    records = [
        _record(i=1, elapsed_ms=100, trace={"otel": {"trace_id": "t1", "span_count": 3}}),
        _record(i=2, elapsed_ms=300, has_error=True),
        _record(i=3, elapsed_ms=200, trace={"otel": {"trace_id": "t3", "span_count": 5}}),
    ]
    agg = aggregate_traces(records)
    assert agg["success_rate"] == 66.7
    assert agg["error_rate"] == 33.3
    assert agg["p50_elapsed_ms"] == 200.0
    assert agg["p95_elapsed_ms"] > 200.0
    assert agg["max_elapsed_ms"] == 300.0
    assert agg["avg_span_count"] == 4.0


async def test_trace_repo_aggregate_empty():
    agg = await InMemoryTraceRepository().aggregate()
    assert agg["total"] == 0 and agg["avg_elapsed_ms"] == 0
    assert agg["success_rate"] == 0.0 and agg["daily"] == []


async def test_trace_repo_query_filters_and_paging():
    repo = InMemoryTraceRepository()
    await repo.add(_record(i=1, intent="metric_query", session_id="A"))
    await repo.add(_record(i=2, intent="diagnosis", session_id="A", has_error=True))
    await repo.add(_record(i=3, intent="metric_query", session_id="B"))

    items, total = await repo.query(session_id="A", limit=10)
    assert total == 2 and len(items) == 2

    items, total = await repo.query(intent="metric_query", limit=10)
    assert total == 2

    items, total = await repo.query(has_error=True, limit=10)
    assert total == 1 and items[0].round_id == "r2"

    page1, total = await repo.query(limit=2, offset=0)
    page2, _ = await repo.query(limit=2, offset=2)
    assert total == 3 and len(page1) == 2 and len(page2) == 1

    agg = await repo.aggregate_filtered(session_id="A")
    assert agg["total"] == 2


# ---- 内存 span 仓储 + 节点汇总 ----


async def test_span_repo_crud():
    repo = InMemorySpanRepository()
    assert await repo.add_many([]) == 0
    assert await repo.list_by_round("s", "r1") == []

    n = await repo.add_many(
        [
            _span(span_id="1" * 16, round_id="r1", name="chat_round", duration_ms=50),
            _span(span_id="2" * 16, round_id="r1", name="node.classify_intent", duration_ms=5),
            _span(span_id="3" * 16, round_id="r2", session_id="s2", duration_ms=9),
        ]
    )
    assert n == 3

    by_round = await repo.list_by_round("s", "r1")
    assert len(by_round) == 2
    assert await repo.list_by_session("s2")
    assert len(await repo.list_recent(10)) == 3

    agg = await repo.aggregate()
    assert agg["total"] == 3
    assert agg["nodes"][0]["name"] == "chat_round"  # 按平均耗时降序


async def test_aggregate_spans():
    agg = aggregate_spans(
        [
            _span(name="node.a", duration_ms=10),
            _span(name="node.a", duration_ms=30, status_code="ERROR"),
            _span(name="node.b", duration_ms=5),
        ]
    )
    assert agg["total"] == 3
    node_a = next(n for n in agg["nodes"] if n["name"] == "node.a")
    assert node_a["count"] == 2 and node_a["errors"] == 1 and node_a["avg_ms"] == 20.0


# ---- OTel span 采集 ----


def _enabled_settings(**kw) -> Settings:
    return Settings(observability=ObservabilitySettings(enabled=True, **kw))


def test_build_round_tracer_disabled_returns_none():
    assert build_round_tracer(Settings(observability=ObservabilitySettings(enabled=False))) is None


def test_round_tracer_produces_otel_spans():
    from dmf_agent.observability import iso_to_ns, otel_meta

    tracer = build_round_tracer(_enabled_settings())
    assert tracer is not None
    assert tracer.start_round(session_id="sess1", user_id="u1", message="hi")
    # 节点 span：用外部时间源作为开始时间（对齐 LangGraph 事件 timestamp）
    handle = tracer.start_node(
        "classify_intent", {"langgraph.step": 1}, start_time_ns=iso_to_ns(datetime.now(timezone.utc))
    )
    handle.end()
    tracer.end_round(attributes={"elapsed_ms": 12})
    spans = tracer.finish()

    assert len(spans) == 2
    # 按开始时间升序：root 在前，节点在后（前端据此画瀑布图）
    root, node = spans[0], spans[1]
    assert root.name == "chat_round" and root.kind == "SERVER"
    assert node.name == "node.classify_intent" and node.kind == "INTERNAL"
    assert root.start_time <= node.start_time
    # OTel 语义：32 位 traceId / 16 位 spanId / 子 span 挂到 root
    assert len(root.trace_id) == 32 and len(root.span_id) == 16
    assert node.trace_id == root.trace_id
    assert node.parent_span_id == root.span_id
    assert root.parent_span_id is None
    assert root.status_code == "OK"
    assert root.attributes["session_id"] == "sess1"
    assert node.attributes["langgraph.step"] == 1
    assert node.duration_ms is not None and node.duration_ms >= 0

    meta = otel_meta(tracer, spans)
    assert meta["trace_id"] == root.trace_id
    assert meta["span_count"] == 2
    assert [n["name"] for n in meta["node_spans"]] == ["node.classify_intent"]


def test_round_tracer_marks_error_status():
    tracer = build_round_tracer(_enabled_settings())
    tracer.start_round(session_id="sess_err")
    handle = tracer.start_node("execute_query")
    handle.end(error=True, message="boom")
    tracer.end_round(error=True, message="boom")
    spans = tracer.finish()
    assert spans[1].status_code == "ERROR"
    assert spans[1].status_message == "boom"
    assert spans[0].status_code == "ERROR"


def test_round_tracer_finish_is_idempotent():
    tracer = build_round_tracer(_enabled_settings())
    tracer.start_round(session_id="sess_idem")
    assert len(tracer.finish()) == 1
    assert tracer.finish() == []


def test_round_tracer_degrades_without_start():
    tracer = build_round_tracer(_enabled_settings())
    handle = tracer.start_node("noop")
    handle.end()  # 未 start_round：空句柄，不应抛错
    assert tracer.finish() == []


# ---- ChatService 端到端：span + trace 落库 ----


async def test_chat_service_persists_spans_and_otel_meta():
    from dmf_agent.agents.metric_agent import AgentDeps, build_metric_agent_graph
    from dmf_agent.db.memory import (
        InMemoryMessageRepository,
        InMemorySessionRepository,
    )
    from dmf_agent.recall import MetricRecaller
    from dmf_agent.semantic.layer import SemanticLayer
    from dmf_agent.services.chat_service import ChatService

    settings = _enabled_settings()
    layer = SemanticLayer()
    graph = build_metric_agent_graph(
        AgentDeps(
            settings=settings,
            get_chat_model=lambda: None,
            semantic_layer=layer,
            recaller=MetricRecaller(),
        )
    )
    span_repo = InMemorySpanRepository()
    trace_repo = InMemoryTraceRepository()
    service = ChatService(
        graph=graph,
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
        trace_repo=trace_repo,
        span_repo=span_repo,
    )

    result = await service.chat(
        session_id=None, user_id="u_otel", message="查一下近7天的GMV趋势"
    )
    otel = result["node_trace"]["otel"]
    assert len(otel["trace_id"]) == 32
    assert otel["span_count"] >= 2
    assert any(n["name"] == "node.classify_intent" for n in otel["node_spans"])

    spans = await span_repo.list_by_round(result["session_id"], result["message_id"])
    names = [s.name for s in spans]
    assert names[0] == "chat_round"
    assert "node.extract_elements" in names
    assert all(s.round_id == result["message_id"] for s in spans)
    assert all(s.session_id == result["session_id"] for s in spans)

    # 轮级 trace 落库（含 otel 元信息）
    record = await trace_repo.get(result["session_id"], result["message_id"])
    assert record is not None
    assert record.trace_id == otel["trace_id"]
    assert record.span_count == otel["span_count"]

    # node_trace 回填轮级观测指标：前端「每条对话的观测信息」直接消费（无需再查观测接口）
    node_trace = result["node_trace"]
    assert node_trace["elapsed_ms"] is not None and node_trace["elapsed_ms"] >= 0
    assert node_trace["otel"]["trace_id"] == otel["trace_id"]
    assert node_trace["otel"]["span_count"] == otel["span_count"]


# ---- obs API（app runtime 使用内存降级仓储） ----


def test_obs_api_after_chat():
    from dmf_agent.api.app import app

    with TestClient(app) as client:
        # 对话一轮，ChatService 把 trace + span 落库
        r = client.post(
            "/api/v1/chat", json={"message": "查一下近7天的GMV趋势", "user_id": "u_obs_t"}
        )
        assert r.status_code == 200
        session_id = r.json()["session_id"]

        # 1) 兼容既有面板
        summary = client.get("/api/v1/obs/summary").json()
        assert set(summary) >= {
            "total", "mock_count", "error_count",
            "avg_elapsed_ms", "intent_distribution", "skill_distribution",
            "success_rate", "p95_elapsed_ms", "daily",
        }
        assert summary["total"] >= 1
        assert "metric_query" in summary["intent_distribution"]

        traces = client.get("/api/v1/obs/traces").json()
        assert traces and {"session_id", "round_id", "intent", "mock_mode"} <= set(traces[0])
        assert "trace_id" in traces[0] and "span_count" in traces[0]

        detail = client.get(
            f"/api/v1/obs/traces/{traces[0]['session_id']}/{traces[0]['round_id']}"
        ).json()
        assert detail["trace"] and detail["session_id"] == traces[0]["session_id"]
        assert isinstance(detail["spans"], list)

        # 2) 专页明细（过滤 + 分页）
        rounds = client.get("/api/v1/obs/rounds", params={"page": 1, "page_size": 10}).json()
        assert rounds["total"] >= 1 and rounds["page"] == 1 and rounds["items"]

        filtered = client.get(
            "/api/v1/obs/rounds", params={"session_id": session_id}
        ).json()
        assert filtered["total"] >= 1

        empty = client.get("/api/v1/obs/rounds", params={"keyword": "不存在的关键词xyz"}).json()
        assert empty["total"] == 0 and empty["items"] == []

        spans = client.get("/api/v1/obs/spans", params={"session_id": session_id}).json()
        assert spans["total"] >= 1
        assert any(n["name"].startswith("node.") for n in spans["nodes"])

        # 3) 会话维度（当前对话的观测信息）
        session_obs = client.get(f"/api/v1/obs/sessions/{session_id}").json()
        assert session_obs["session_id"] == session_id
        assert session_obs["summary"]["total"] >= 1
        assert len(session_obs["rounds"]) >= 1
        assert client.get("/api/v1/obs/sessions/not-exist").status_code == 404

        # 4) 导出（保留能力，不依赖外部平台）
        export = client.get("/api/v1/obs/export", params={"session_id": session_id}).json()
        assert "resourceSpans" in export  # 默认 OTLP/JSON
        export_json = client.get(
            "/api/v1/obs/export", params={"session_id": session_id, "format": "json"}
        ).json()
        assert export_json["rounds"] and export_json["otel_semconv"]

        # health：观测走 OTel 落库，不再有 OTLP 导出开关
        components = client.get("/health").json()["components"]
        assert components["trace_store"] in ("memory", "mysql")
        assert components["otel_tracing"] in ("on", "off")

        # 历史消息还原：助手 node_trace 同样带观测信息（前端切会话后仍可展示）
        msgs = client.get(f"/api/v1/sessions/{session_id}/messages").json()
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        assert assistant["node_trace"].get("otel", {}).get("trace_id")
        assert assistant["node_trace"].get("elapsed_ms") is not None


def test_obs_round_detail_404():
    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/v1/obs/traces/nope/nope")
        assert r.status_code == 404


def test_obs_export_otlp_round_fields():
    """导出单轮：OTLP 结构字段符合规范命名（traceId/spanId/startTimeUnixNano）。"""
    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.post("/api/v1/chat", json={"message": "GMV", "user_id": "u_exp"})
        data = r.json()
        payload = client.get(
            "/api/v1/obs/export",
            params={
                "session_id": data["session_id"],
                "round_id": data["message_id"],
            },
        ).json()
        assert payload["resourceSpans"]
        scope = payload["resourceSpans"][0]["scopeSpans"][0]
        span = scope["spans"][0]
        assert set(span) >= {
            "traceId", "spanId", "parentSpanId", "name", "kind",
            "startTimeUnixNano", "endTimeUnixNano", "attributes", "status",
        }
        assert len(span["traceId"]) == 32

        # 带 download=true 时返回附件头
        resp = client.get(
            "/api/v1/obs/export",
            params={"session_id": data["session_id"], "download": "true"},
        )
        assert "attachment" in resp.headers.get("content-disposition", "")
