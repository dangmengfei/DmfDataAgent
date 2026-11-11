"""Sub-Agent 编排协议层（PRD FR10）：无业务依赖的纯数据结构。

单独成模块以打破 `subagent <-> decomposer` 的循环导入：
- `subagent.py` 与 `decomposer.py` 均从此处导入协议类型，
  本模块只依赖标准库，不引用任何业务模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SubTaskStatus(str, Enum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class SubTask:
    """一个子任务（Orchestrator 派发给 Worker 的最小工作单元）。"""

    id: str
    question: str
    intent: str = "metric_query"
    depth: int = 0
    parent_id: Optional[str] = None
    # 可选作用域提示（仅元数据；子问题文本本身已限定范围）
    hints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SubResult:
    """一个子任务的执行结果（Worker 回报 Orchestrator）。"""

    id: str
    question: str
    status: SubTaskStatus
    depth: int = 0
    analysis: Optional[Dict[str, Any]] = None
    query_result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    parent_trace_id: Optional[str] = None
    elapsed_ms: Optional[float] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class SubAgentContext:
    """Worker 运行所需的最小上下文（隔离用）。

    只携带身份与子任务，不携带任何主图消息历史，从而实现上下文最小权限隔离。
    """

    session_id: str
    user_id: str = ""
    tenant_id: str = ""
    user_role: str = ""
    user_permissions: List[str] = field(default_factory=list)
    parent_trace_id: Optional[str] = None
