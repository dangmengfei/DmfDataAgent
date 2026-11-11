"""取数节点：语义查询计划 -> MetricFlow 编译 SQL -> StarRocks 执行。

主流程已完全脱离指标平台，也没有任何 mock 分支：
- DMF_STARROCKS-* 未配置或连接/查询失败 -> 返回带 error 的空结果（明确失败，不造假数据）；
- 编译错误（指标未收录 / 维度不被支持）-> 同样以 error 结果返回，由答复节点向用户解释。

产出 state.query_result：
{columns, rows, sql, engine, models, group_columns, metric_columns,
 row_count, truncated, elapsed_ms, mock: false}
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState
from dmf_agent.harness import HarnessContext, HarnessState

logger = logging.getLogger(__name__)


def _extract_elements(dsl: Dict[str, Any]) -> tuple[list[str], list[str], list[Dict[str, Any]]]:
    """从 DSL 提取指标/维度/过滤，用于审计与脱敏。

    指标在 DSL v3 中为 [{"name": "GMV", ...}]，归一化为标准名列表，便于审计聚合。
    """
    raw_metrics = dsl.get("metrics") or []
    metrics = [
        m.get("name") if isinstance(m, dict) else m
        for m in raw_metrics
    ]
    metrics = [m for m in metrics if m]
    dimensions = dsl.get("dimensions") or []
    filters = dsl.get("filters") or []
    return metrics, dimensions, filters


def build_execute_query_node(deps: AgentDeps):
    async def execute_query(state: AgentState) -> Dict[str, Any]:
        dsl = state.get("dsl") or {}
        engine = deps.metric_engine
        if engine is None:
            message = "语义引擎未装配，无法取数（请检查应用启动日志）"
            logger.error(message)
            return {"query_result": {"columns": [], "rows": [], "mock": False, "error": message}}

        harness = getattr(deps, "harness", None)
        harness_state_raw = state.get("harness_state") or {}
        harness_state = HarnessState(
            rounds=harness_state_raw.get("rounds", 0),
            tool_calls=harness_state_raw.get("tool_calls", 0),
            tokens_used=harness_state_raw.get("tokens_used", 0),
        )
        hctx = HarnessContext(
            session_id=state.get("session_id") or "",
            user_id=state.get("user_id") or "",
            tenant_id=state.get("tenant_id") or "",
            round_id=state.get("round_id") or "",
            user_permissions=list(state.get("user_permissions") or []),
        )

        # FR13 行权限下推上下文（enforce 时由授权加载填充；audit/off 时保持 None）
        row_scopes = None

        async def _raw_query() -> Dict[str, Any]:
            # FR13 P6 行权限下推：row_scopes 非空时注入 SQL 最内层 WHERE
            return await engine.query(dsl, row_scopes=row_scopes)

        async def _do_query() -> Dict[str, Any]:
            if harness is not None:
                return await harness.invoke(
                    "execute_query", {"dsl": dsl}, hctx, harness_state, _raw_query
                )
            return await _raw_query()

        # FR13 数据权限（P5 加载 + P6 enforce）：从授权仓储加载用户/角色权限上下文，
        # 审计越权风险；enforce 模式下剔除越权指标 / 下推行权限 / 过滤被禁维度列。
        metrics_pre, dims_pre, filters_pre = _extract_elements(dsl)
        security = getattr(deps, "security", None)
        perm_ctx = None
        if security is not None:
            try:
                perm_ctx = await security.build_permission_context(
                    user_id=state.get("user_id") or "",
                    tenant_id=state.get("tenant_id") or "",
                    role=state.get("user_role") or "",
                )
                await security.audit_permission_check(
                    perm_ctx,
                    metrics_pre,
                    dims_pre,
                    filters_pre,
                    session_id=state.get("session_id") or "",
                    round_id=state.get("round_id") or "",
                )
            except Exception:  # noqa: BLE001 - 权限上下文加载/审计失败不阻断取数
                logger.warning("数据权限上下文加载/审计失败（跳过）", exc_info=True)

        # FR13 P6 enforce：指标白名单剔除 + 行权限下推准备
        if security is not None and perm_ctx is not None and security.permission_mode == "enforce":
            allowed, denied = security.filter_metrics(perm_ctx, metrics_pre)
            if denied:
                logger.info("数据权限 enforce：剔除越权指标 %s", denied)
                dsl = {**dsl, "metrics": allowed}
                metrics_pre, dims_pre, filters_pre = _extract_elements(dsl)
                if not allowed:
                    denied_list = ", ".join(sorted(denied))
                    message = (
                        f"当前账号无权访问所请求的全部指标（被权限策略拦截：{denied_list}）。"
                        "请联系管理员授予相应指标权限。"
                    )
                    return {
                        "query_result": {
                            "columns": [],
                            "rows": [],
                            "mock": False,
                            "error": message,
                            "error_code": "E3003",
                            "error_category": 3,
                        }
                    }
            if perm_ctx.row_scopes:
                row_scopes = perm_ctx.row_scopes

        # PRD FR6：数据源瞬时错误限次自愈（退避重查最多 datasource_max_retries 次）。
        # 分类为 DataSourceError 才重试；权限/编译等不可重试错误直接返回友好错误结果。
        from dmf_agent.agents.metric_agent.errors import build_error_sample
        from dmf_agent.agents.metric_agent.recovery import recover_datasource

        result, ds_error, attempts = await recover_datasource(
            _do_query, deps.recovery_settings, node="execute_query", on_recovery=None
        )
        if ds_error is not None:
            # 仅在最终有错误时采样一次（自愈成功 recovered=True / 最终失败 recovered=False）
            repo = getattr(deps, "error_sample_repo", None)
            if repo is not None:
                ctx = {
                    "session_id": state.get("session_id") or "",
                    "user_id": state.get("user_id") or "",
                    "tenant_id": state.get("tenant_id") or "",
                    "round_id": state.get("round_id") or "",
                    "node": "execute_query",
                    "trace_snippet": (str(dsl.get("metrics") or ""))[:500],
                }
                await repo.add(
                    build_error_sample(
                        ds_error, recovered=(result is not None), attempts=attempts, ctx=ctx
                    )
                )
            if result is None:
                logger.error("MetricFlow 取数失败（已重试 %d 次）：%s", attempts, ds_error.code)
                return {
                    "query_result": {
                        "columns": [],
                        "rows": [],
                        "mock": False,
                        "engine": "metricflow",
                        "error": ds_error.user_message,
                        "error_code": ds_error.code,
                        "error_category": int(ds_error.category),
                    }
                }
            # 自愈成功：result 可用，继续后续脱敏 / 审计

        if not result.get("columns"):
            logger.warning("取数成功但无列信息：rows=%s", len(result.get("rows") or []))

        # P0-2 结果脱敏：按列名自动识别敏感列并掩码
        if security is not None:
            result = security.mask_query_result(result)

        # FR13 P6 enforce：列 deny 硬剔除（区别于脱敏掩码——掩码仍返回脱敏值，deny 直接不出列）
        if security is not None and perm_ctx is not None and security.permission_mode == "enforce":
            kept, removed = security.filter_result_columns(perm_ctx, result.get("columns") or [])
            if removed:
                logger.info("数据权限 enforce：剔除被禁维度列 %s", removed)
                idxs = [i for i, c in enumerate(result["columns"]) if c in kept]
                result["columns"] = kept
                result["rows"] = [[row[i] for i in idxs] for row in (result.get("rows") or [])]

        # P0-2 审计：数据访问留痕（复用取数前提取的 metrics/dims）
        if security is not None:
            await security.audit_data_access(
                session_id=state.get("session_id") or "",
                user_id=state.get("user_id") or "",
                tenant_id=state.get("tenant_id") or "",
                round_id=state.get("round_id") or "",
                metrics=metrics_pre,
                dimensions=dims_pre,
                row_count=int(result.get("row_count") or 0),
                elapsed_ms=float(result.get("elapsed_ms") or 0),
                sql=str(result.get("sql") or ""),
                detail={"truncated": bool(result.get("truncated")), "engine": result.get("engine")},
            )

        logger.info(
            "取数完成（engine=%s models=%s rows=%s elapsed=%sms truncated=%s）",
            result.get("engine"),
            result.get("models"),
            result.get("row_count"),
            result.get("elapsed_ms"),
            result.get("truncated"),
        )
        return {"query_result": result, "harness_state": harness_state.snapshot()}

    return execute_query
