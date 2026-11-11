"""Harness 执行控制器（PRD FR15：循环控制、预算、超时、白名单、强制埋点、输出约束）。

用法：每个可能访问数据/外部资源的节点动作通过 `harness.invoke(name, args, ctx)` 调用；
Harness 先做白名单/参数/权限/预算校验，通过后再执行真实动作，最后按声明 output_schema
强制解析结果（失败重试或交由上层降级）并记录 span 与审计。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from dmf_agent.config.settings import HarnessSettings
from dmf_agent.harness.registry import ToolRegistry

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class HarnessViolationError(Exception):
    """Harness 拦截的安全/预算/越权/输出 Schema 异常。"""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


async def enforce_output_schema(
    model: Type[BaseModel],
    data: Any,
    *,
    retries: int = 0,
    fn: Optional[Callable[[], Awaitable[Any]]] = None,
) -> BaseModel:
    """FR15 输出约束：将动作/LLM 输出按声明 Schema 解析。

    - 解析成功：返回校验后的 pydantic 实例（字段已规范化）。
    - 解析失败且有 ``fn``：最多重试 ``retries`` 次（重新执行 ``fn`` 取新输出）。
    - 全部失败：抛 :class:`HarnessViolationError`（交由上层降级，如回退规则/模板），
      满足 PRD「解析失败重试或降级；禁止输出自由格式指令」。
    """
    payload = data
    retries = max(0, retries)
    for attempt in range(1 + retries):
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            if fn is not None and attempt < retries:
                payload = await fn()
                continue
            raise HarnessViolationError(
                "HARNESS_OUTPUT_SCHEMA",
                f"输出不符合声明 Schema {getattr(model, '__name__', model)}"
                f"（已重试 {retries} 次）：{exc}",
                {"schema": getattr(model, "__name__", str(model)), "error": str(exc)},
            ) from exc
    # 不可达：循环必在耗尽前 return 或 raise
    raise HarnessViolationError(
        "HARNESS_OUTPUT_SCHEMA",
        "输出 Schema 校验循环异常退出",
        {"schema": getattr(model, "__name__", str(model))},
    )


@dataclass
class HarnessContext:
    session_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    user_permissions: List[str] = field(default_factory=list)
    round_id: str = ""


@dataclass
class HarnessState:
    """单轮运行时的 Harness 计数器（随 AgentState 携带）。"""

    rounds: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    violations: List[Dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tokens_used": self.tokens_used,
            "violations": list(self.violations),
            "elapsed_seconds": round(time.time() - self.started_at, 3),
        }


class HarnessController:
    def __init__(
        self,
        settings: HarnessSettings,
        registry: Optional[ToolRegistry] = None,
        auditor: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.auditor = auditor

    def _check_whitelist(self, name: str) -> None:
        if not self.settings.enforce_whitelist:
            return
        if self.registry is None:
            return
        if not self.registry.is_allowed(name):
            raise HarnessViolationError(
                "HARNESS_TOOL_NOT_ALLOWED",
                f"动作 '{name}' 未在 Harness 工具白名单注册，执行被拒绝",
                {"tool": name},
            )

    def _check_arguments(self, name: str, arguments: Dict[str, Any]) -> None:
        if self.registry is None:
            return
        spec = self.registry.get(name)
        if spec is None:
            return
        validation = self.registry.validate_arguments(name, arguments)
        if not validation["ok"]:
            raise HarnessViolationError(
                "HARNESS_INVALID_ARGUMENTS",
                f"动作 '{name}' 参数校验失败：{validation['error']}",
                {"tool": name, "validation_error": validation["error"]},
            )

    def _check_permissions(self, name: str, ctx: HarnessContext) -> None:
        if self.registry is None:
            return
        spec = self.registry.get(name)
        if spec is None:
            return
        # 通配放行：admin 角色持有 ["*"]，与 RBAC role_has_permission 语义一致
        if "*" in ctx.user_permissions:
            return
        missing = [p for p in spec.required_permissions if p not in ctx.user_permissions]
        if missing:
            raise HarnessViolationError(
                "HARNESS_PERMISSION_DENIED",
                f"动作 '{name}' 需要权限 {missing}",
                {"tool": name, "missing_permissions": missing},
            )

    def _check_budget(self, state: HarnessState) -> None:
        if self.settings.max_rounds > 0 and state.rounds > self.settings.max_rounds:
            raise HarnessViolationError(
                "HARNESS_MAX_ROUNDS",
                f"超过最大 ReAct 轮数限制 {self.settings.max_rounds}",
                {"max_rounds": self.settings.max_rounds},
            )
        if self.settings.max_tool_calls > 0 and state.tool_calls > self.settings.max_tool_calls:
            raise HarnessViolationError(
                "HARNESS_MAX_TOOL_CALLS",
                f"超过最大工具调用次数 {self.settings.max_tool_calls}",
                {"max_tool_calls": self.settings.max_tool_calls},
            )
        if self.settings.token_budget > 0 and state.tokens_used > self.settings.token_budget:
            raise HarnessViolationError(
                "HARNESS_TOKEN_BUDGET",
                f"超过 token 预算 {self.settings.token_budget}",
                {"token_budget": self.settings.token_budget},
            )

    def _check_timeout(self, state: HarnessState) -> None:
        if self.settings.node_timeout_seconds <= 0:
            return
        elapsed = time.time() - state.started_at
        if elapsed >= self.settings.node_timeout_seconds:
            raise HarnessViolationError(
                "HARNESS_TIMEOUT",
                f"运行超时（限制 {self.settings.node_timeout_seconds}s，已运行 {elapsed:.1f}s）",
                {"timeout_seconds": self.settings.node_timeout_seconds, "elapsed": elapsed},
            )

    async def _emit_audit(
        self,
        ctx: HarnessContext,
        name: str,
        started: float,
        *,
        status: str,
        elapsed_ms: Optional[int] = None,
        error: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not (self.settings.force_telemetry and self.auditor):
            return
        if elapsed_ms is None:
            elapsed_ms = int((time.time() - started) * 1000)
        event = {
            "event_type": "harness_tool_call",
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "round_id": ctx.round_id,
            "tool": name,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "arguments_keys": list((arguments or {}).keys()),
        }
        if error is not None:
            event["error"] = error
        try:
            await self.auditor(event)
        except Exception:  # noqa: BLE001 - 审计失败不阻断主链路
            logger.warning("Harness 审计写入失败", exc_info=True)

    async def invoke(
        self,
        name: str,
        arguments: Dict[str, Any],
        ctx: HarnessContext,
        state: HarnessState,
        fn: Callable[[], Awaitable[Dict[str, Any]]],
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行一次受约束动作。"""
        if not self.settings.enabled:
            return await fn()

        state.rounds += 1
        state.tool_calls += 1
        self._check_whitelist(name)
        self._check_permissions(name, ctx)
        self._check_arguments(name, arguments)
        self._check_budget(state)
        self._check_timeout(state)

        spec = self.registry.get(name) if self.registry else None
        timeout = timeout_seconds or self.settings.node_timeout_seconds or None

        async def _run() -> Dict[str, Any]:
            if timeout and timeout > 0:
                return await asyncio.wait_for(fn(), timeout=timeout)
            return await fn()

        started = time.time()
        try:
            result = await _run()

            # ---- FR15 输出约束：按声明 output_schema 强制解析 ----
            if spec is not None and spec.output_schema is not None:
                retries = spec.output_retries if spec.output_retries > 0 else self.settings.output_retries
                try:
                    validated = await enforce_output_schema(
                        spec.output_schema, result, retries=retries, fn=_run
                    )
                except HarnessViolationError as ve:
                    await self._emit_audit(
                        ctx, name, started, status="output_schema_error", error=str(ve), arguments=arguments
                    )
                    raise
                result = validated.model_dump()
            # ----------------------------------------------------

            elapsed = round(time.time() - started, 3)
            await self._emit_audit(
                ctx, name, started, status="success", elapsed_ms=int(elapsed * 1000), arguments=arguments
            )
            return result
        except HarnessViolationError:
            raise
        except asyncio.TimeoutError:
            raise HarnessViolationError(
                "HARNESS_TIMEOUT",
                f"动作 '{name}' 执行超时",
                {"tool": name, "timeout_seconds": timeout},
            ) from None
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.time() - started, 3)
            await self._emit_audit(
                ctx, name, started, status="error", error=str(exc), arguments=arguments
            )
            raise

    def record_tokens(self, state: HarnessState, tokens: int) -> None:
        if tokens <= 0:
            return
        state.tokens_used += tokens
        if self.settings.token_budget > 0 and state.tokens_used >= self.settings.token_budget:
            raise HarnessViolationError(
                "HARNESS_TOKEN_BUDGET",
                f"token 预算超限（预算 {self.settings.token_budget}，已用 {state.tokens_used}）",
                {"token_budget": self.settings.token_budget, "tokens_used": state.tokens_used},
            )
