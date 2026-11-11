"""Agent 执行错误分类与可读解释（PRD FR6 报错分析处理）。

错误码分段（1xxx–5xxx），与 PRD 的「解析 / 编译 / 权限 / 数据源 / 执行」五类一一对应：

- 1xxx 解析错误（ParseError）    ：意图 / 要素 LLM 结构化失败，**可自动修复**（规则兜底）
- 2xxx 编译错误（CompileError）  ：DSL 组装 / 语义层解析失败，确定性、**不可重试**
- 3xxx 权限错误（AuthorizationError）：Harness 越权 / 工具被拒
- 4xxx 数据源错误（DataSourceError）：StarRocks 连接 / 查询失败，**瞬时可重试**
- 5xxx 执行错误（ExecutionError） ：节点运行时 / 超时 / 未知异常

所有节点 / 服务层捕获到的异常，最终都会通过 `classify_exception` 收敛为一个
`AgentError` 子类，携带 `code / category / user_message / retryable / fix_hint`，
供「限次自动修复重试」「人工可读解释」「入观测样本池」三件事复用同一套结构。
"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any, Dict, Optional

from dmf_agent.harness import HarnessViolationError


class ErrorCategory(IntEnum):
    """错误大类（与错误码首位一致：1=解析 2=编译 3=权限 4=数据源 5=执行）。"""

    PARSE = 1
    COMPILE = 2
    PERMISSION = 3
    DATASOURCE = 4
    EXECUTION = 5


# 分类中文标签（前端 / 观测展示用）
CATEGORY_LABELS: Dict[int, str] = {
    int(ErrorCategory.PARSE): "解析",
    int(ErrorCategory.COMPILE): "编译",
    int(ErrorCategory.PERMISSION): "权限",
    int(ErrorCategory.DATASOURCE): "数据源",
    int(ErrorCategory.EXECUTION): "执行",
}


# 按分类的通用人工可读解释（具体错误可覆盖）
CATEGORY_USER_MESSAGE: Dict[ErrorCategory, str] = {
    ErrorCategory.PARSE: "问题理解失败，已尝试自动修正；若结果仍不准请换种说法或补充指标/时间。",
    ErrorCategory.COMPILE: "查询计划编译失败（指标/维度不被支持或口径冲突），请检查所选指标与维度。",
    ErrorCategory.PERMISSION: "当前账号无权执行该操作或未授权访问相关指标，请联系管理员。",
    ErrorCategory.DATASOURCE: "数据源暂时不可用或查询超时，已自动重试；如持续失败请稍后重试或联系数据团队。",
    ErrorCategory.EXECUTION: "服务内部执行异常，请稍后重试；若反复出现请反馈给运维。",
}


def _exc_name(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__name__}"


def _looks_like_datasource(exc: BaseException) -> bool:
    """启发式识别数据源（StarRocks / MySQL）相关异常，用于判定可重试。

    命中条件：异常来自 aiomysql / pymysql / sqlalchemy 驱动，或类名属于
    连接 / 操作性错误；asyncio 超时（DB 查询超时）也归为数据源瞬时错误。
    """
    name = _exc_name(exc)
    if "aiomysql" in name or "pymysql" in name or "sqlalchemy" in name:
        return True
    cls = type(exc).__name__
    if cls in {
        "OperationalError",
        "InterfaceError",
        "DatabaseError",
        "DBAPIError",
        "ConnectionError",
        "ConnectionResetError",
        "TimeoutError",
    }:
        return True
    return False


class AgentError(Exception):
    """统一的 Agent 执行错误（所有子类共享结构化字段）。"""

    def __init__(
        self,
        category: ErrorCategory,
        code: str,
        user_message: str,
        *,
        detail: Optional[str] = None,
        retryable: bool = False,
        fix_hint: Optional[str] = None,
        node: Optional[str] = None,
    ) -> None:
        super().__init__(user_message)
        self.category = category
        self.code = code
        self.user_message = user_message
        self.detail = detail
        self.retryable = retryable
        self.fix_hint = fix_hint
        self.node = node

    def to_dict(self) -> Dict[str, Any]:
        """结构化信息，供 trace / 错误事件 / 样本池复用。"""
        return {
            "code": self.code,
            "category": int(self.category),
            "category_label": CATEGORY_LABELS[int(self.category)],
            "message": self.user_message,
            "detail": self.detail,
            "retryable": self.retryable,
            "fix_hint": self.fix_hint,
            "node": self.node,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.user_message}"


class ParseError(AgentError):
    """解析错误（1xxx）：LLM 结构化输出失败，可自动修复为规则兜底。"""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E1001",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.PARSE,
            code,
            message or CATEGORY_USER_MESSAGE[ErrorCategory.PARSE],
            detail=detail,
            retryable=True,
            fix_hint="reparse_rule",
            node=node,
        )


class CompileError(AgentError):
    """编译错误（2xxx）：DSL/语义层解析失败，确定性不可重试。"""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E2001",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.COMPILE,
            code,
            message or CATEGORY_USER_MESSAGE[ErrorCategory.COMPILE],
            detail=detail,
            retryable=False,
            fix_hint=None,
            node=node,
        )


class AuthorizationError(AgentError):
    """权限错误（3xxx）：Harness 越权 / 工具被拒。"""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E3001",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.PERMISSION,
            code,
            message or CATEGORY_USER_MESSAGE[ErrorCategory.PERMISSION],
            detail=detail,
            retryable=False,
            fix_hint=None,
            node=node,
        )


class DataSourceError(AgentError):
    """数据源错误（4xxx）：StarRocks 连接/查询失败，瞬时可重试。"""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E4001",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.DATASOURCE,
            code,
            message or CATEGORY_USER_MESSAGE[ErrorCategory.DATASOURCE],
            detail=detail,
            retryable=True,
            fix_hint="retry_query",
            node=node,
        )


class ExecutionError(AgentError):
    """执行错误（5xxx）：节点运行时 / 超时 / 未知异常。"""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E5001",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.EXECUTION,
            code,
            message or CATEGORY_USER_MESSAGE[ErrorCategory.EXECUTION],
            detail=detail,
            retryable=False,
            fix_hint=None,
            node=node,
        )


class SessionBusyError(AgentError):
    """会话占用错误（5xxx）：同一会话已有另一轮在跑（共享账号 / 多标签页并发）。

    由会话串行化闸（services/session_lock.py）在等锁超时后触发，语义上可重试
    （用户等本轮结束再发即可）。
    """

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: str = "E5005",
        node: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(
            ErrorCategory.EXECUTION,
            code,
            message or "该会话正在回复中，请等本轮结束再发送（同一会话不支持并发提问）。",
            detail=detail,
            retryable=True,
            fix_hint="retry_later",
            node=node,
        )


def classify_exception(exc: BaseException, *, node: Optional[str] = None) -> AgentError:
    """把任意原始异常收敛为结构化 `AgentError`（已是 AgentError 则回写 node）。

    分类优先级：
    1. 已是 AgentError -> 直接沿用（补充 node）
    2. HarnessViolationError -> 按 code 分权限 / 超时 / 其它执行
    3. 数据源特征异常 -> DataSourceError（可重试）
    4. 结构化解析异常（JSON/值/键/类型）-> ParseError（可重试）
    5. 兜底 -> ExecutionError
    """
    if isinstance(exc, AgentError):
        if exc.node is None and node:
            exc.node = node
        return exc

    if isinstance(exc, HarnessViolationError):
        code = exc.code or ""
        if "PERMISSION" in code or "DENIED" in code:
            return AuthorizationError(
                exc.message, code="E3001", node=node, detail=json.dumps(exc.details, ensure_ascii=False)
            )
        if "TIMEOUT" in code:
            return ExecutionError(
                exc.message, code="E5002", node=node, detail=json.dumps(exc.details, ensure_ascii=False)
            )
        return ExecutionError(
            exc.message, code="E5003", node=node, detail=json.dumps(exc.details, ensure_ascii=False)
        )

    if _looks_like_datasource(exc):
        return DataSourceError(
            str(exc) or "数据源连接/查询失败", code="E4001", node=node, detail=repr(exc)
        )

    if isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
        return ParseError(
            str(exc) or "结构化解析失败", code="E1003", node=node, detail=repr(exc)
        )

    return ExecutionError(str(exc) or "未知执行错误", code="E5004", node=node, detail=repr(exc))


def build_error_sample(
    error: AgentError,
    *,
    recovered: bool,
    attempts: int,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """构造一条「观测样本池」记录（供 `ErrorSampleRepository.add` 消费）。"""
    return {
        "session_id": ctx.get("session_id") or "",
        "user_id": ctx.get("user_id") or "",
        "tenant_id": ctx.get("tenant_id") or "",
        "round_id": ctx.get("round_id") or "",
        "error_code": error.code,
        "category": int(error.category),
        "category_label": CATEGORY_LABELS[int(error.category)],
        "node": error.node or ctx.get("node") or "",
        "message": error.user_message,
        "detail": error.detail or "",
        "retryable": bool(error.retryable),
        "recovered": bool(recovered),
        "fix_hint": error.fix_hint or "",
        "attempts": int(attempts),
        "trace_snippet": ctx.get("trace_snippet") or "",
    }
