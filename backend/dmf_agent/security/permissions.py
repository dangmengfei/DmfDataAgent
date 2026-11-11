"""数据权限引擎（PRD FR13：RBAC + 行级/列级/指标级权限下推）。

P0-2 阶段采用 **audit-only** 模式：只检测并记录越权风险，不拦截查询（由 `mode` 控制）。
P6 enforce 阶段会把结果中的越权列过滤 / 行权限下推到 SQL WHERE。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set


@dataclass
class UserPermissionContext:
    user_id: str = ""
    tenant_id: str = ""
    roles: List[str] = field(default_factory=list)
    # 用户被授予访问的指标 key 集合（空表示暂无限制）
    allowed_metrics: Set[str] = field(default_factory=set)
    # 用户被禁止访问的维度/列
    denied_dimensions: Set[str] = field(default_factory=set)
    # 行权限：用户只能看这些维度值（key=维度名，value=允许值集合）
    row_scopes: Dict[str, Set[str]] = field(default_factory=dict)


class DataPermissionEngine:
    def __init__(self, mode: str = "audit") -> None:
        # "audit"：只出报告不拦截；"enforce"：拦截/过滤
        self.mode = mode

    def check_query(
        self,
        ctx: UserPermissionContext,
        metrics: List[str],
        dimensions: List[str],
        filters: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """检查一次查询请求的权限风险。返回报告。"""
        findings: List[Dict[str, Any]] = []

        # 指标级
        for metric in metrics:
            if ctx.allowed_metrics and metric not in ctx.allowed_metrics:
                findings.append(
                    {
                        "type": "metric_unauthorized",
                        "level": "high",
                        "metric": metric,
                        "message": f"用户无权访问指标 {metric}",
                    }
                )

        # 列级
        for dim in dimensions:
            if dim in ctx.denied_dimensions:
                findings.append(
                    {
                        "type": "dimension_denied",
                        "level": "high",
                        "dimension": dim,
                        "message": f"用户被禁止查看维度 {dim}",
                    }
                )

        # 行级：检查过滤条件是否超出用户授权范围
        for scope_dim, allowed_values in ctx.row_scopes.items():
            # 若查询已按该维度过滤，检查值是否在授权范围内
            matched = next((f for f in filters if (f.get("dimension") or f.get("column")) == scope_dim), None)
            if matched is not None:
                vals = matched.get("values") or matched.get("value") or []
                if not isinstance(vals, list):
                    vals = [vals]
                unauthorized = [v for v in vals if v not in allowed_values]
                if unauthorized:
                    findings.append(
                        {
                            "type": "row_scope_violation",
                            "level": "high",
                            "dimension": scope_dim,
                            "unauthorized_values": unauthorized,
                            "message": f"用户无权限查看 {scope_dim}={unauthorized}",
                        }
                    )

        blocked = self.mode == "enforce" and any(f["level"] == "high" for f in findings)
        return {
            "mode": self.mode,
            "blocked": blocked,
            "findings": findings,
            "allowed_metrics": sorted(ctx.allowed_metrics) if ctx.allowed_metrics else [],
            "denied_dimensions": sorted(ctx.denied_dimensions) if ctx.denied_dimensions else [],
            "row_scope_dimensions": sorted(ctx.row_scopes.keys()) if ctx.row_scopes else [],
        }

    def check_result_rows(
        self,
        ctx: UserPermissionContext,
        columns: List[str],
        rows: List[List[Any]],
    ) -> List[Dict[str, Any]]:
        """审计结果行：检查是否出现未授权维度列（audit-only）。"""
        findings: List[Dict[str, Any]] = []
        denied_in_result = [c for c in columns if c in ctx.denied_dimensions]
        if denied_in_result:
            findings.append(
                {
                    "type": "result_contains_denied_column",
                    "level": "high",
                    "columns": denied_in_result,
                    "message": f"结果列包含用户无权查看的字段：{denied_in_result}",
                }
            )
        return findings

    def filter_metrics(
        self, ctx: "UserPermissionContext", metrics: List[str]
    ) -> "tuple[List[str], List[str]]":
        """enforce 指标白名单：返回 `(allowed, denied)`。

        - `allowed_metrics` 为空（无授权记录）= **不限制**，全部 `allowed`。
        - 否则只保留白名单内的指标，其余标记为 `denied`（应被从查询 DSL 剔除）。
        """
        if not ctx.allowed_metrics:
            return list(metrics), []
        allowed = [m for m in metrics if m in ctx.allowed_metrics]
        denied = [m for m in metrics if m not in ctx.allowed_metrics]
        return allowed, denied

    def filter_result_columns(
        self, ctx: "UserPermissionContext", columns: List[str]
    ) -> "tuple[List[str], List[str]]":
        """enforce 列 deny：从结果集中**硬剔除** `denied_dimensions` 的列。

        与脱敏（mask）不同——mask 仍返回脱敏值，deny 是直接不出该列。
        返回 `(kept_columns, removed_columns)`。
        """
        if not ctx.denied_dimensions:
            return list(columns), []
        removed = [c for c in columns if c in ctx.denied_dimensions]
        kept = [c for c in columns if c not in ctx.denied_dimensions]
        return kept, removed
