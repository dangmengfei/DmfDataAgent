"""安全层协调器：输入/输出守卫 + 脱敏 + 权限审计 + 审计日志一站式入口。

PRD FR13 落地：
- 输入侧：prompt injection / 越权意图 / 敏感词预检；
- 输出侧：LLM 输出内容安全过滤 + PII 脱敏；
- 结果集：按列名自动脱敏；
- 权限：audit-only 检测指标/维度/行权限风险并记录审计（P0-2 不拦截）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import SecuritySettings
from dmf_agent.security.auditor import AuditLogger, AuditSink
from dmf_agent.security.desensitizer import Desensitizer
from dmf_agent.security.guard import ContentGuard
from dmf_agent.security.permissions import DataPermissionEngine, UserPermissionContext
from dmf_agent.security.policy_loader import load_permission_context


class SecurityCoordinator:
    """安全层统一入口。"""

    def __init__(
        self,
        settings: SecuritySettings,
        audit_sink: Optional[AuditSink] = None,
        grant_repo: Any = None,
        scope_repo: Any = None,
        policy_repo: Any = None,
    ) -> None:
        self.settings = settings
        self.guard = ContentGuard(extra_sensitive_words=settings.sensitive_words)
        self.desensitizer = Desensitizer(enabled=settings.desensitize_enabled)
        self.permission_engine = DataPermissionEngine(mode=settings.permission_mode)
        self.audit_logger = AuditLogger(sink=audit_sink, enabled=True)
        # FR13 P5/P6 授权仓储（enforce 用；None = 无授权数据，等价于"不限制"）
        self.grant_repo = grant_repo
        self.scope_repo = scope_repo
        self.policy_repo = policy_repo

    async def build_permission_context(
        self, *, user_id: str, tenant_id: str, role: str
    ) -> UserPermissionContext:
        """从授权仓储加载该主体的权限上下文（指标白名单 / 行权限 / 列 deny）。

        enforce 引擎与审计都消费这个 ctx；任何仓储不可用都安全降级为"不限制"。
        """
        return await load_permission_context(
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            grant_repo=self.grant_repo,
            scope_repo=self.scope_repo,
            policy_repo=self.policy_repo,
        )

    @property
    def permission_mode(self) -> str:
        """当前权限模式：off | audit | enforce。"""
        return self.permission_engine.mode

    def filter_metrics(
        self, ctx: UserPermissionContext, metrics: List[str]
    ) -> "tuple[List[str], List[str]]":
        """enforce 指标白名单：剔除越权指标，返回 (allowed, denied)。"""
        return self.permission_engine.filter_metrics(ctx, metrics)

    def filter_result_columns(
        self, ctx: UserPermissionContext, columns: List[str]
    ) -> "tuple[List[str], List[str]]":
        """enforce 列 deny：从结果集硬剔除被禁维度列，返回 (kept, removed)。"""
        return self.permission_engine.filter_result_columns(ctx, columns)

    def check_input(self, text: str) -> Dict[str, Any]:
        if not self.settings.enabled or not self.settings.input_guard_enabled:
            return {"blocked": False, "reason": "", "risk_level": "none", "hits": []}
        return self.guard.check_input(text, max_length=self.settings.max_input_length)

    def check_output(self, text: str) -> Dict[str, Any]:
        if not self.settings.enabled or not self.settings.output_guard_enabled:
            return {"safe": True, "hits": [], "sanitized_text": text}
        return self.guard.check_output(text)

    def mask_text(self, text: str) -> str:
        return self.desensitizer.mask_text(text)

    def mask_query_result(self, query_result: Dict[str, Any]) -> Dict[str, Any]:
        return self.desensitizer.mask_query_result(query_result)

    async def audit_permission_check(
        self,
        ctx: UserPermissionContext,
        metrics: List[str],
        dimensions: List[str],
        filters: List[Dict[str, Any]],
        *,
        session_id: str = "",
        round_id: str = "",
    ) -> Dict[str, Any]:
        report = self.permission_engine.check_query(ctx, metrics, dimensions, filters)
        for f in report.get("findings") or []:
            await self.audit_logger.security(
                event_type="permission/check",
                level=f["level"],
                summary=f["message"],
                session_id=session_id,
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                round_id=round_id,
                detail={
                    "finding_type": f["type"],
                    "metric": f.get("metric"),
                    "dimension": f.get("dimension"),
                    "unauthorized_values": f.get("unauthorized_values"),
                    "permission_mode": self.settings.permission_mode,
                },
            )
        return report

    async def audit_data_access(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str,
        round_id: str,
        metrics: list[str],
        dimensions: list[str],
        row_count: int,
        elapsed_ms: float,
        sql: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.data_access(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            round_id=round_id,
            metrics=metrics,
            dimensions=dimensions,
            row_count=row_count,
            elapsed_ms=elapsed_ms,
            sql=sql,
            detail=detail,
        )

    async def audit_security(
        self,
        *,
        event_type: str,
        level: str,
        summary: str,
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        round_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.security(
            event_type=event_type,
            level=level,
            summary=summary,
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            round_id=round_id,
            detail=detail,
        )

    async def audit_memory_operation(
        self,
        *,
        operation: str,
        layer: str,
        user_id: str,
        tenant_id: str,
        target: str = "",
        session_id: str = "",
        round_id: str = "",
        summary: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.memory_operation(
            operation=operation,
            layer=layer,
            user_id=user_id,
            tenant_id=tenant_id,
            target=target,
            session_id=session_id,
            round_id=round_id,
            summary=summary,
            detail=detail,
        )

    async def audit_system_bootstrap(
        self,
        *,
        action: str,
        tenant_id: str = "",
        user_id: str = "",
        target: str = "",
        summary: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.system_bootstrap(
            action=action,
            tenant_id=tenant_id,
            user_id=user_id,
            target=target,
            summary=summary,
            detail=detail,
        )

    async def audit_metric_creation(
        self,
        *,
        action: str,
        metric_key: str,
        name: str,
        user_id: str,
        tenant_id: str,
        status: str = "draft",
        version: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.metric_creation(
            action=action,
            metric_key=metric_key,
            name=name,
            user_id=user_id,
            tenant_id=tenant_id,
            status=status,
            version=version,
            detail=detail,
        )

    async def audit_token_usage(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str,
        round_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model: str = "",
        cost: float = 0.0,
        intent: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        return await self.audit_logger.token_usage(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            round_id=round_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=model,
            cost=cost,
            intent=intent,
            detail=detail,
        )
