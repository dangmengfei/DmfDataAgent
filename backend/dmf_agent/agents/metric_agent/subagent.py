"""Sub-Agent 编排（PRD FR10）：Orchestrator–Worker 模式。

把一个复杂问题**分解**为多个子任务（SubTask），由**子 Agent Worker** 并行执行
（每个 Worker 跑一段聚焦的指标问答子图：要素抽取 -> 校验 -> 编译 -> 取数 -> 分析），
最后把各子结果**合并**回主答复。

设计约束（对应 FR10）：
- Orchestrator–Worker 模式：本模块是 Orchestrator，Worker 复用现有节点工厂。
- `SubTask` / `SubResult` 协议：结构化输入 / 输出。
- 上下文最小权限隔离：Worker 只拿到子问题 + 身份（角色 / 权限），
  **不继承主图消息历史**（state.messages 强制为空）。
- 并行调度与超时回收：`asyncio.gather` + `asyncio.Semaphore(max_concurrency)`，
  每个子任务 `asyncio.wait_for(timeout)`，超时回收为 `timeout` 状态（不影响其它子任务）。
- 深度 ≤ max_depth / 并发上限：`max_subtasks` + `max_depth` 双重护栏。
- 子 Agent 独立埋点：每个子任务创建独立 `RoundTracer`（独立 trace_id），便于按子任务回溯。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from dmf_agent.agents.metric_agent.decomposer import decompose_query
from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.errors import classify_exception
from dmf_agent.agents.metric_agent.nodes import (
    build_analyze_data_node,
    build_build_dsl_node,
    build_execute_query_node,
    build_extract_elements_node,
    build_validate_elements_node,
)
from dmf_agent.agents.metric_agent.subagent_protocol import (
    SubAgentContext,
    SubResult,
    SubTask,
    SubTaskStatus,
)
from dmf_agent.config.settings import SubAgentSettings
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.observability import build_round_tracer

logger = logging.getLogger(__name__)


# ---------------- Worker ----------------


class SubAgentWorker:
    """聚焦的指标问答子图 Worker（Orchestrator–Worker 中的 Worker 角色）。

    复用现有节点工厂（extract_elements / validate_elements / build_dsl /
    execute_query / analyze_data），在**一段独立的最小状态**上顺序执行，
    产出 `analysis` 与 `query_result` 供 Orchestrator 合并。
    """

    def __init__(self, deps: AgentDeps) -> None:
        self._deps = deps
        self._extract = build_extract_elements_node(deps)
        self._validate = build_validate_elements_node(deps)
        self._build_dsl = build_build_dsl_node(deps)
        self._execute = build_execute_query_node(deps)
        self._analyze = build_analyze_data_node(deps)

    def _build_state(self, task: SubTask, ctx: SubAgentContext) -> AgentState:
        # 最小权限隔离：只放子问题与身份，不继承任何父消息历史（messages 强制为空）
        return {
            "query_original": task.question,
            "query_rewritten": task.question,
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "user_role": ctx.user_role,
            "user_permissions": list(ctx.user_permissions),
            "round_id": ctx.session_id,
            "harness_state": {},
            "security_blocked": False,
            "security_block_reason": "",
            "intent": task.intent,
            # 关键隔离：不传入主图 messages，避免子任务读到无关上下文
            "messages": [],
        }

    async def run(
        self,
        task: SubTask,
        ctx: SubAgentContext,
        tracer: Any = None,
    ) -> SubResult:
        started = time.time()
        started_iso = datetime.now(timezone.utc).isoformat()
        trace_id = getattr(tracer, "trace_id", None) if tracer is not None else None
        try:
            state = self._build_state(task, ctx)
            for node in (
                self._extract,
                self._validate,
                self._build_dsl,
                self._execute,
                self._analyze,
            ):
                update = await node(state)
                if isinstance(update, dict):
                    state.update(update)
            return SubResult(
                id=task.id,
                question=task.question,
                status=SubTaskStatus.OK,
                depth=task.depth,
                analysis=state.get("analysis"),
                query_result=state.get("query_result"),
                trace_id=trace_id,
                parent_trace_id=ctx.parent_trace_id,
                elapsed_ms=round((time.time() - started) * 1000, 1),
                started_at=started_iso,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - 子任务失败不影响其它子任务
            error = classify_exception(exc, node="subagent_worker")
            logger.warning("SubTask %s 执行失败：%s", task.id, error.code)
            return SubResult(
                id=task.id,
                question=task.question,
                status=SubTaskStatus.FAILED,
                depth=task.depth,
                error=error.to_dict(),
                trace_id=trace_id,
                parent_trace_id=ctx.parent_trace_id,
                elapsed_ms=round((time.time() - started) * 1000, 1),
                started_at=started_iso,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )


# ---------------- Orchestrator ----------------


async def dispatch_subtasks(
    tasks: List[SubTask],
    *,
    deps: AgentDeps,
    ctx: SubAgentContext,
    settings: Optional[SubAgentSettings] = None,
    tracer_factory: Optional[Callable[[], Any]] = None,
    worker: Optional[Any] = None,
) -> List[SubResult]:
    """并行调度多个子任务（Orchestrator 核心）。

    - 并发上限：`Semaphore(settings.max_concurrency)`
    - 子任务超时回收：`asyncio.wait_for(timeout=subtask_timeout_seconds)`，超时 -> `timeout`
    - 深度护栏：depth > max_depth 直接 `skipped`（不执行）
    - 数量护栏：超过 max_subtasks 的尾部任务 `skipped`
    - 独立埋点：每个子任务用 tracer_factory 创建独立 tracer（独立 trace_id）
    - 失败隔离：单个子任务异常 -> `failed`，不影响其它子任务
    """
    cfg = settings or (
        deps.subagent_settings if deps.settings is not None else SubAgentSettings()
    )
    worker = worker or SubAgentWorker(deps)
    factory = tracer_factory or (lambda: build_round_tracer(deps.settings))
    # 确保至少 1，避免 Semaphore(0) 死锁
    semaphore = asyncio.Semaphore(max(1, cfg.max_concurrency))

    results: List[Optional[SubResult]] = [None] * len(tasks)

    async def _run_one(idx: int, task: SubTask, skip: bool) -> None:
        if skip:
            results[idx] = SubResult(
                id=task.id,
                question=task.question,
                status=SubTaskStatus.SKIPPED,
                depth=task.depth,
                parent_trace_id=ctx.parent_trace_id,
                error={
                    "code": "E1007",
                    "category": 1,
                    "message": "超过最大子任务数，已跳过",
                    "detail": f"max_subtasks={cfg.max_subtasks}",
                },
            )
            return
        if task.depth > cfg.max_depth:
            results[idx] = SubResult(
                id=task.id,
                question=task.question,
                status=SubTaskStatus.SKIPPED,
                depth=task.depth,
                parent_trace_id=ctx.parent_trace_id,
                error={
                    "code": "E1006",
                    "category": 1,
                    "message": "超过最大递归深度，已跳过",
                    "detail": f"depth={task.depth}>{cfg.max_depth}",
                },
            )
            return

        tracer = None
        trace_id: Optional[str] = None
        if factory is not None:
            try:
                tracer = factory()
                if tracer is not None:
                    tracer.start_round(
                        session_id=ctx.session_id,
                        user_id=ctx.user_id,
                        message=task.question,
                    )
                    trace_id = getattr(tracer, "trace_id", None)
            except Exception:  # noqa: BLE001 - tracer 失败不影响子任务
                tracer = None

        async def _bound() -> SubResult:
            try:
                return await asyncio.wait_for(
                    worker.run(task, ctx, tracer=tracer),
                    timeout=cfg.subtask_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "SubTask %s 超时（>%ss）回收", task.id, cfg.subtask_timeout_seconds
                )
                return SubResult(
                    id=task.id,
                    question=task.question,
                    status=SubTaskStatus.TIMEOUT,
                    depth=task.depth,
                    parent_trace_id=ctx.parent_trace_id,
                    trace_id=trace_id,
                    error={
                        "code": "E5005",
                        "category": 5,
                        "message": "子任务执行超时",
                        "detail": f"timeout>{cfg.subtask_timeout_seconds}s",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 子任务异常隔离
                error = classify_exception(exc, node="subagent_dispatch")
                return SubResult(
                    id=task.id,
                    question=task.question,
                    status=SubTaskStatus.FAILED,
                    depth=task.depth,
                    parent_trace_id=ctx.parent_trace_id,
                    trace_id=trace_id,
                    error=error.to_dict(),
                )
            finally:
                if tracer is not None:
                    try:
                        tracer.finish()
                    except Exception:  # noqa: BLE001
                        pass

        async with semaphore:
            results[idx] = await _bound()

    jobs = [
        _run_one(i, t, skip=(i >= cfg.max_subtasks)) for i, t in enumerate(tasks)
    ]
    await asyncio.gather(*jobs)
    return [r for r in results if r is not None]


# ---------------- 合并 ----------------


def merge_subresults(results: List[SubResult]) -> Dict[str, Any]:
    """把多个子结果合并为供主答复使用的数据结构。

    返回字段：
    - ok / failed / timed_out / skipped / total 计数
    - summaries：每个成功子任务的摘要（带子问题前缀，便于聚合答复区分来源）
    - analyses / query_results：原始子结果列表
    - combined_summary：换行拼接的摘要文本
    """
    summaries: List[str] = []
    analyses: List[Dict[str, Any]] = []
    query_results: List[Dict[str, Any]] = []
    ok = failed = timed_out = skipped = 0
    for r in results:
        if r.status == SubTaskStatus.OK:
            ok += 1
            if r.analysis:
                analyses.append(r.analysis)
                s = r.analysis.get("summary")
                if s:
                    summaries.append(f"【{r.question}】{s}")
            if r.query_result:
                query_results.append(r.query_result)
        elif r.status == SubTaskStatus.TIMEOUT:
            timed_out += 1
        elif r.status == SubTaskStatus.SKIPPED:
            skipped += 1
        else:
            failed += 1
    return {
        "ok": ok,
        "failed": failed,
        "timed_out": timed_out,
        "skipped": skipped,
        "total": len(results),
        "summaries": summaries,
        "analyses": analyses,
        "query_results": query_results,
        "combined_summary": "\n".join(summaries),
    }


def build_merged_analysis(merged: Dict[str, Any]) -> Dict[str, Any]:
    """由合并结果构造一个 `analysis` 结构，供 synthesize_answer 直接渲染。

    synthesize_answer 读取 `analysis.summary` 与 `analysis.stats`；这里把多个
    子任务摘要聚合进 summary，并标注来源数量，便于前端区分多子任务聚合答复。
    """
    return {
        "summary": merged["combined_summary"] or "（子任务未产出可分析的明细数据）",
        "stats": {},
        "subagent": True,
        "subagent_counts": {
            "ok": merged["ok"],
            "failed": merged["failed"],
            "timed_out": merged["timed_out"],
            "skipped": merged["skipped"],
            "total": merged["total"],
        },
    }


def result_to_dict(result: SubResult) -> Dict[str, Any]:
    """SubResult -> 可序列化字典（落库 / 前端展示）。"""
    return asdict(result)


# ---------------- 主图集成节点（Opt-in） ----------------


def build_subagent_dispatch_node(deps: AgentDeps):
    """主图可选节点：在取数链路起点判断是否分解为多子任务并行编排。

    行为：
    - `subagent.enabled=False` -> 完全透传（返回 {}，主图走常规单链路）
    - 分解结果 ≤1 个子任务 -> 透传（主图常规路径处理，避免重复取数）
    - 分解出多子任务 -> 派发并行执行，合并结果注入 `analysis` / `query_result`，
      并置 `subagent_dispatched=True`，主图随后跳过常规取数直接进 synthesize_answer
    - 全部子任务失败 -> 透传（由主链路常规路径给出错误解释）

    该节点**默认接入主图**（`Settings.subagent.wire_into_graph=True`）：
    单任务 / 未识别到拆分时节点完全透传，不改变既有单图链路与回归测试；
    仅当问题被规则分解为 ≥2 个子任务时才走并行编排路径。
    """

    async def subagent_dispatch(state: AgentState) -> Dict[str, Any]:
        cfg = deps.subagent_settings
        if not cfg.enabled:
            return {}
        question = user_query(state)
        tasks = decompose_query(question, deps=deps, max_subtasks=cfg.max_subtasks)
        if len(tasks) <= 1:
            return {}

        ctx = SubAgentContext(
            session_id=state.get("session_id") or "",
            user_id=state.get("user_id") or "",
            tenant_id=state.get("tenant_id") or "",
            user_role=state.get("user_role") or "",
            user_permissions=list(state.get("user_permissions") or []),
        )
        results = await dispatch_subtasks(tasks, deps=deps, ctx=ctx, settings=cfg)
        merged = merge_subresults(results)
        if merged["ok"] == 0:
            logger.info("子任务全部未成功，退回主图常规路径")
            return {}
        return {
            "subagent_results": [result_to_dict(r) for r in results],
            "analysis": build_merged_analysis(merged),
            "query_result": (
                merged["query_results"][-1] if merged["query_results"] else None
            ),
            "subagent_dispatched": True,
        }

    return subagent_dispatch
