"""请求级语义层作用域（ContextVar）：让租户 overlay 在并发下安全生效。

为什么必须是 ContextVar，而不是给 ``deps`` 赋值
------------------------------------------------
图节点由 ``build_xxx_node(deps)`` 闭包持有**同一个** ``AgentDeps`` 实例，并在
调用时读 ``deps.semantic_layer``（这正是热更新"就地改写即生效"的原因）。但反过来看，
**所有请求共享同一个 deps 对象** —— 若按请求直接写 ``deps.semantic_layer = 租户层``，
并发请求会互相串台（A 请求刚写完，B 请求就覆盖，两边都用错口径），而且失败时无法回滚。

因此这里不改变量本身，而是在 ContextVar 上记一份"本请求生效的层"：
``AgentDeps.semantic_layer`` / ``.metric_engine`` 变成 property —— 读时先看 ContextVar
覆盖、再回落进程级基座；写时仍落到基座存储（热更新语义不变）。

- asyncio 任务各自持有独立 context → 请求之间天然隔离；
- LangGraph 内部即便用 ``create_task`` 派生执行，context 也是**复制**而非共享，
  覆盖在整个子任务树里都可见（复制只影响"子任务里的写入不外泄"）；
- 复位必须严格在 ``finally`` 里做：残留的租户作用域会让后续请求取到别人的口径，
  这是**数据正确性**问题而非体验问题。

本模块刻意**零项目依赖**（只用标准库）：``agents`` / ``services`` / ``api`` 都会引用它，
放在 semantic 包底部可避免任何循环导入。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestScope:
    """一个请求内生效的语义层（含引擎与溯源信息）。

    ``layer`` / ``engine`` 为该租户解析后的实例；``resolved`` 是 ``ResolvedLayer``
    （请求源 / 生效源 / 版本链），供观测与排查"这轮用的是哪份定义"。
    """

    tenant_id: str = ""
    layer: Any = None
    engine: Any = None
    resolved: Any = None

    @property
    def active(self) -> bool:
        return self.layer is not None

    def describe(self) -> dict:
        """轻量描述（不展开层内容），用于日志与 trace。"""
        chain = getattr(self.resolved, "chain", None) or []
        return {
            "tenant_id": self.tenant_id,
            "scope": getattr(self.resolved, "scope", ""),
            "base_version": getattr(self.resolved, "base_version", None),
            "overlay_versions": list(getattr(self.resolved, "overlay_versions", []) or []),
            "chain": [f"{c.get('scope')}:v{c.get('version')}" for c in chain],
        }


_SCOPE: ContextVar[Optional[RequestScope]] = ContextVar("dmf_semantic_scope", default=None)


def current_scope() -> Optional[RequestScope]:
    """当前请求生效的作用域；未绑定（后台任务 / 全局路径）返回 None。"""
    return _SCOPE.get()


def bind_scope(scope: Optional[RequestScope]) -> Optional[Token]:
    """绑定作用域，返回用于复位的 token（``scope`` 为 None 时返回 None）。"""
    if scope is None:
        return None
    return _SCOPE.set(scope)


def reset_scope(token: Optional[Token]) -> None:
    """复位到绑定前状态。

    两处失败都必须**吞掉而不是抛出** —— 本函数只会在 ``finally`` 里被调用，
    从 ``finally`` 抛出的异常会顶替掉真正的业务异常，把根因藏起来：

    - ``ValueError``：token 与当前 context 不同源（异步生成器被提前关闭/跨任务收尾）。
      此时**宁可清空也不能残留租户作用域**（残留 = 取到别的租户的口径）。
    - ``RuntimeError``：token 已被使用过（重复复位）。作用域早已复位，无需再动。
    """
    if token is None:
        return
    try:
        _SCOPE.reset(token)
    except ValueError:  # pragma: no cover - 跨 context 复位的兜底
        _SCOPE.set(None)
    except RuntimeError:  # pragma: no cover - 重复复位（幂等）
        logger.debug("语义层作用域 token 已复位过，忽略重复复位")


@asynccontextmanager
async def use_scope(scope: Optional[RequestScope]) -> AsyncIterator[Optional[RequestScope]]:
    """``with`` 形式的作用域（同步代码块 / 非流式路径用）；``scope=None`` 时为空操作。"""
    token = bind_scope(scope)
    try:
        yield scope
    finally:
        reset_scope(token)
