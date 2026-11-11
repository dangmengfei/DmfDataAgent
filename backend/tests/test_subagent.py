"""P1-2 Sub-Agent 编排（PRD FR10）单元测试。

覆盖：
- Worker 在最小隔离状态上跑通聚焦子图（要素->校验->编译->取数->分析）
- 上下文最小权限隔离（不继承主图消息历史）
- 并行调度 + 并发上限（Semaphore）
- 子任务超时回收（wait_for -> timeout，不影响其它子任务）
- 深度护栏（depth > max_depth -> skipped）
- 数量护栏（超过 max_subtasks -> skipped）
- 子 Agent 独立埋点（每子任务独立 trace_id）
- 合并结果（merge_subresults）
- 问题分解（单任务不拆分 / 多指标并列拆分）
- 主图集成节点透传（enabled=False 或单任务时返回 {}）
"""

from __future__ import annotations

import asyncio

import pytest

from dmf_agent.agents.metric_agent.decomposer import decompose_query
from dmf_agent.agents.metric_agent.subagent import (
    SubAgentContext,
    SubAgentWorker,
    SubResult,
    SubTask,
    SubTaskStatus,
    build_merged_analysis,
    build_subagent_dispatch_node,
    dispatch_subtasks,
    merge_subresults,
)
from dmf_agent.config.settings import SubAgentSettings
from fakes import make_deps


# ---------------- Fake 辅助 ----------------


class _FakeTracer:
    def __init__(self) -> None:
        self.calls = 0
        self.trace_id: str | None = None

    def start_round(self, **kw) -> bool:
        self.calls += 1
        self.trace_id = f"trace-{self.calls}-{id(self)}"
        return True

    def finish(self):
        return None


class _SleepyWorker:
    """可控耗时的假 Worker：按 task.id 决定是否慢 / 慢多久。"""

    def __init__(self, slow_ids=(), slow_seconds=0.0) -> None:
        self._slow_ids = set(slow_ids)
        self._slow_seconds = slow_seconds

    async def run(self, task, ctx, tracer=None):
        if task.id in self._slow_ids:
            await asyncio.sleep(self._slow_seconds)
        else:
            await asyncio.sleep(0.01)
        tid = getattr(tracer, "trace_id", None) if tracer is not None else None
        return SubResult(
            id=task.id,
            question=task.question,
            status=SubTaskStatus.OK,
            trace_id=tid,
            parent_trace_id=ctx.parent_trace_id,
        )


class _ConcurrencyWorker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.current = 0
        self.max_seen = 0

    async def run(self, task, ctx, tracer=None):
        async with self._lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
        await asyncio.sleep(0.05)
        async with self._lock:
            self.current -= 1
        tid = getattr(tracer, "trace_id", None) if tracer is not None else None
        return SubResult(
            id=task.id,
            question=task.question,
            status=SubTaskStatus.OK,
            trace_id=tid,
            parent_trace_id=ctx.parent_trace_id,
        )


def _ctx() -> SubAgentContext:
    return SubAgentContext(session_id="s1", user_id="u1", tenant_id="t1")


# ---------------- Worker：聚焦子图 + 隔离 ----------------


@pytest.mark.asyncio
async def test_worker_runs_focused_chain():
    deps = make_deps()
    worker = SubAgentWorker(deps)
    task = SubTask(id="st-1", question="查询 GMV 的走势", intent="metric_query")
    res = await worker.run(task, _ctx(), tracer=None)
    assert res.status == SubTaskStatus.OK
    assert res.analysis is not None
    assert res.analysis.get("summary")
    assert res.query_result is not None
    assert "GMV" in res.query_result.get("columns", [])


@pytest.mark.asyncio
async def test_worker_isolation_no_parent_messages():
    """Worker 只消费子问题，不继承主图历史（state.messages 强制为空）。"""
    deps = make_deps()
    worker = SubAgentWorker(deps)
    # 子问题只提到 付费用户数，不提及 GMV
    task = SubTask(id="st-1", question="查询 付费用户数 的表现", intent="metric_query")
    res = await worker.run(task, _ctx(), tracer=None)
    assert res.status == SubTaskStatus.OK
    cols = res.query_result.get("columns", [])
    assert "付费用户数" in cols
    # 隔离保证：不会混入无关指标
    assert "GMV" not in cols


@pytest.mark.asyncio
async def test_worker_classifies_exception():
    deps = make_deps()
    worker = SubAgentWorker(deps)

    async def _boom(state):
        raise ValueError("boom")

    worker._extract = _boom  # 强制第一个节点失败
    res = await worker.run(SubTask(id="st-1", question="x"), _ctx(), tracer=None)
    assert res.status == SubTaskStatus.FAILED
    assert res.error is not None
    assert res.error["category"] in (1, 2)  # 解析/编译类


# ---------------- 并行调度 + 并发上限 ----------------


@pytest.mark.asyncio
async def test_dispatch_parallel_concurrency_cap():
    deps = make_deps()
    worker = _ConcurrencyWorker()
    tasks = [SubTask(id=f"st-{i}", question=f"q{i}") for i in range(4)]
    settings = SubAgentSettings(max_concurrency=2, subtask_timeout_seconds=5)
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=_ctx(), settings=settings,
        tracer_factory=lambda: None, worker=worker,
    )
    assert len(results) == 4
    assert all(r.status == SubTaskStatus.OK for r in results)
    # 并发上限生效：峰值并发 == 2（4 个任务，Semaphore(2)）
    assert worker.max_seen == 2


# ---------------- 超时回收 ----------------


@pytest.mark.asyncio
async def test_dispatch_timeout_recovery():
    deps = make_deps()
    worker = _SleepyWorker(slow_ids={"st-1"}, slow_seconds=0.5)
    tasks = [
        SubTask(id="st-1", question="slow"),
        SubTask(id="st-2", question="fast"),
    ]
    settings = SubAgentSettings(max_concurrency=4, subtask_timeout_seconds=0.1)
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=_ctx(), settings=settings,
        tracer_factory=lambda: None, worker=worker,
    )
    by_id = {r.id: r for r in results}
    assert by_id["st-1"].status == SubTaskStatus.TIMEOUT
    assert by_id["st-2"].status == SubTaskStatus.OK


# ---------------- 深度 / 数量护栏 ----------------


@pytest.mark.asyncio
async def test_dispatch_depth_guard():
    deps = make_deps()
    worker = _SleepyWorker()
    tasks = [
        SubTask(id="st-0", question="q0", depth=0),
        SubTask(id="st-3", question="q3", depth=3),  # 超过 max_depth=2
    ]
    settings = SubAgentSettings(max_depth=2, subtask_timeout_seconds=5)
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=_ctx(), settings=settings,
        tracer_factory=lambda: None, worker=worker,
    )
    by_id = {r.id: r for r in results}
    assert by_id["st-0"].status == SubTaskStatus.OK
    assert by_id["st-3"].status == SubTaskStatus.SKIPPED
    assert by_id["st-3"].error["code"] == "E1006"


@pytest.mark.asyncio
async def test_dispatch_max_subtasks_cap():
    deps = make_deps()
    worker = _SleepyWorker()
    tasks = [SubTask(id=f"st-{i}", question=f"q{i}") for i in range(10)]
    settings = SubAgentSettings(max_subtasks=3, subtask_timeout_seconds=5)
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=_ctx(), settings=settings,
        tracer_factory=lambda: None, worker=worker,
    )
    ok = [r for r in results if r.status == SubTaskStatus.OK]
    skipped = [r for r in results if r.status == SubTaskStatus.SKIPPED]
    assert len(ok) == 3
    assert len(skipped) == 7
    assert all(r.error and r.error["code"] == "E1007" for r in skipped)


# ---------------- 独立埋点 ----------------


@pytest.mark.asyncio
async def test_independent_tracing_per_subtask():
    deps = make_deps()
    worker = _SleepyWorker()
    tracers = []

    def factory():
        t = _FakeTracer()
        tracers.append(t)
        return t

    tasks = [SubTask(id=f"st-{i}", question=f"q{i}") for i in range(3)]
    settings = SubAgentSettings(subtask_timeout_seconds=5)
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=_ctx(), settings=settings,
        tracer_factory=factory, worker=worker,
    )
    trace_ids = [r.trace_id for r in results]
    assert len(trace_ids) == 3
    # 每子任务独立 trace_id（互不重复）
    assert len(set(trace_ids)) == 3
    # 父 trace_id 透传
    assert all(r.parent_trace_id is None for r in results)  # _ctx 未设 parent
    # tracer.start_round 被调用
    assert all(t.calls == 1 for t in tracers)


@pytest.mark.asyncio
async def test_parent_trace_id_propagated():
    deps = make_deps()
    worker = _SleepyWorker()
    ctx = SubAgentContext(session_id="s1", parent_trace_id="parent-xyz")
    tasks = [SubTask(id="st-1", question="q")]
    results = await dispatch_subtasks(
        tasks, deps=deps, ctx=ctx,
        settings=SubAgentSettings(subtask_timeout_seconds=5),
        tracer_factory=lambda: None, worker=worker,
    )
    assert results[0].parent_trace_id == "parent-xyz"


# ---------------- 合并 ----------------


def test_merge_subresults():
    results = [
        SubResult(id="a", question="Qa", status=SubTaskStatus.OK,
                  analysis={"summary": "A 摘要"}),
        SubResult(id="b", question="Qb", status=SubTaskStatus.OK,
                  analysis={"summary": "B 摘要"}),
        SubResult(id="c", question="Qc", status=SubTaskStatus.TIMEOUT,
                  error={"code": "E5005"}),
        SubResult(id="d", question="Qd", status=SubTaskStatus.SKIPPED),
    ]
    merged = merge_subresults(results)
    assert merged["ok"] == 2
    assert merged["timed_out"] == 1
    assert merged["skipped"] == 1
    assert merged["total"] == 4
    assert "A 摘要" in merged["combined_summary"]
    assert "B 摘要" in merged["combined_summary"]
    analysis = build_merged_analysis(merged)
    assert analysis["subagent"] is True
    assert analysis["subagent_counts"]["ok"] == 2


# ---------------- 问题分解 ----------------


def test_decompose_single_not_split():
    deps = make_deps()
    tasks = decompose_query("查询 gmv_amount 的走势", deps=deps, max_subtasks=6)
    assert len(tasks) == 1


def test_decompose_multi_split():
    deps = make_deps()
    # 含并列连词"和"且识别到 ≥2 个指标（中文显示名）-> 拆分
    tasks = decompose_query(
        "对比 GMV 和 订单数 的表现", deps=deps, max_subtasks=6
    )
    assert len(tasks) == 2
    assert all(t.depth == 0 for t in tasks)
    # 子问题应各自锚定一个指标
    assert any("GMV" in t.question for t in tasks)
    assert any("订单数" in t.question for t in tasks)


# ---------------- 主图集成节点透传 ----------------


@pytest.mark.asyncio
async def test_dispatch_node_passthrough_when_disabled():
    deps = make_deps()
    deps.settings.subagent.enabled = False
    node = build_subagent_dispatch_node(deps)
    out = await node({"session_id": "s", "query_rewritten": "查询 gmv_amount"})
    assert out == {}


@pytest.mark.asyncio
async def test_dispatch_node_passthrough_when_single():
    deps = make_deps()
    # 单任务（无拆分标记/单指标）-> 透传，由主图常规路径处理
    node = build_subagent_dispatch_node(deps)
    out = await node({"session_id": "s", "query_rewritten": "查询 GMV 走势"})
    assert out == {}


@pytest.mark.asyncio
async def test_subagent_wired_into_graph_end_to_end():
    """开启 wire_into_graph 后，多指标并列问题走子 Agent 并行编排并聚合答复。"""
    from langchain_core.messages import HumanMessage

    from dmf_agent.agents.metric_agent.graph import build_metric_agent_graph

    deps = make_deps()
    deps.settings.subagent.wire_into_graph = True
    graph = build_metric_agent_graph(deps)

    state = {
        "messages": [HumanMessage(content="对比 GMV 和 订单数 的表现")],
        "session_id": "s1",
        "user_id": "u1",
        "tenant_id": "",
        "user_role": "",
        "user_permissions": [],
        "round_id": "s1",
        "harness_state": {},
        "security_blocked": False,
        "security_block_reason": "",
    }
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "s1"}})
    # 子 Agent 已派发并合并结果
    assert result.get("subagent_dispatched") is True
    assert result.get("subagent_results") is not None
    # 聚合答复应覆盖两个子任务的指标
    assert "GMV" in (result.get("final_answer") or "")
    assert "订单数" in (result.get("final_answer") or "")


@pytest.mark.asyncio
async def test_subagent_wired_by_default():
    """FR10 接主图：默认配置（不再手工置 wire_into_graph）即把多指标并列问题接入子 Agent 编排。"""
    from langchain_core.messages import HumanMessage

    from dmf_agent.agents.metric_agent.graph import build_metric_agent_graph

    deps = make_deps()
    # 不显式置位：断言默认值已为 True（接主图）
    assert deps.settings.subagent.wire_into_graph is True
    graph = build_metric_agent_graph(deps)

    state = {
        "messages": [HumanMessage(content="对比 GMV 和 订单数 的表现")],
        "session_id": "s2",
        "user_id": "u2",
        "tenant_id": "",
        "user_role": "",
        "user_permissions": [],
        "round_id": "s2",
        "harness_state": {},
        "security_blocked": False,
        "security_block_reason": "",
    }
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": "s2"}})
    assert result.get("subagent_dispatched") is True
    assert result.get("subagent_results") is not None
    assert "GMV" in (result.get("final_answer") or "")
    assert "订单数" in (result.get("final_answer") or "")


def test_template_answer_renders_subagent_aggregation():
    """mock 模式下，多子任务聚合答复应渲染逐子任务明细并覆盖各指标。"""
    from dmf_agent.agents.metric_agent.nodes.synthesize_answer import _template_answer

    state = {
        "intent": "metric_query",
        "analysis": {
            "subagent": True,
            "subagent_counts": {"ok": 2, "failed": 0, "timed_out": 0, "skipped": 0, "total": 2},
        },
        "subagent_results": [
            {
                "id": "st-1",
                "question": "GMV在相关维度下的表现如何？",
                "status": "ok",
                "analysis": {"summary": "GMV 摘要"},
                "query_result": {
                    "engine": "metricflow", "row_count": 1, "elapsed_ms": 5,
                    "columns": ["指标"], "rows": [["GMV"]],
                },
            },
            {
                "id": "st-2",
                "question": "订单数在相关维度下的表现如何？",
                "status": "ok",
                "analysis": {"summary": "订单数 摘要"},
                "query_result": {
                    "engine": "metricflow", "row_count": 1, "elapsed_ms": 5,
                    "columns": ["指标"], "rows": [["订单数"]],
                },
            },
        ],
    }
    out = _template_answer(state)
    assert "多子任务" in out
    assert "GMV" in out
    assert "订单数" in out
    # 逐子任务标题与数据表均渲染
    assert "子任务 1" in out
    assert "子任务 2" in out
