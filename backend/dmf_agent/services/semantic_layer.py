"""语义层热更新：不重启进程即让新的发布快照生效。

为什么可以就地改写
------------------
所有图节点都通过 ``deps.semantic_layer`` **在调用时**读取语义层
（``layer = deps.semantic_layer`` 写在节点函数体内，不是构图期绑定的闭包变量），
``AgentDeps`` 又是普通 dataclass —— 因此就地改写 ``deps.semantic_layer`` /
``deps.metric_engine`` 即可让后续每一轮问答立即用上新定义。

注意：这两个字段是 property（见 ``semantic/scope.py``），**写**落到进程级基座、
**读**优先看请求级 ContextVar 覆盖。所以热更新改的是基座，而"某租户本轮用哪个层"
由 ``SemanticScopeService`` 按请求决定，两者互不干扰。

两条生效路径
------------
- **推**：管理面 ``POST /semantic/registry/reload``（以及发布 / 审批 / 回滚后自动触发）
  调本模块的 ``refresh_semantic_layer``，本进程主动重新解析；
- **拉**：``SemanticScopeService`` 每次请求做一次节流探测，发现**全局链指纹变了**
  （典型场景：另一个实例发布了新的 full 快照）就地重建基座 —— 于是多实例部署
  不必依赖广播，各实例下一请求自动收敛。两条路径共用 ``apply_resolved``。

并发与失败语义
--------------
- 刷新走 ``asyncio.Lock`` 串行化（同一时刻只允许一次重载，避免两把刷新互相覆盖）；
- **刷新失败保留旧层**：重新解析失败时抛错但不动运行时对象，正在服务的进程继续可用，
  绝不出现"刷新把线上语义层弄没了"的状态；
- 刷新成功后同步 ``SemanticScopeService.rebase()``（换基座 + 清租户缓存）。
"""

from __future__ import annotations

import logging
from typing import Optional

from dmf_agent.semantic.engine import MetricFlowEngine
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.semantic.runtime import ResolvedLayer, resolve_layer

logger = logging.getLogger(__name__)


def _lock_for(runtime) -> Optional[object]:
    return getattr(runtime, "semantic_refresh_lock", None)


def apply_resolved(
    runtime,
    resolved: ResolvedLayer,
    engine: Optional[MetricFlowEngine],
) -> None:
    """把一份**已解析**结果回写到运行时持有物（不重新解析、不碰租户缓存）。

    抽成公共函数是因为有两条完全不同的调用路径都要做同一件事：

    1. ``_do_refresh``（管理面热更新 / 审批通过后自动触发）：解析 → 回写 → rebase 租户缓存；
    2. ``SemanticScopeService`` 的基座自愈（"拉"式发现别实例的发布）：它自己就是租户缓存的
       持有者，回调进来只能回写 runtime/deps，**绝不能**再调 rebase —— 那会变成
       ``rebase → on_base_changed → rebase`` 自环。

    因此本函数刻意**不含** rebase 步骤，由调用方按需补上。
    """
    runtime.semantic_layer = resolved.layer
    runtime.metric_engine = engine
    runtime.semantic_resolution = resolved

    deps = getattr(runtime, "agent_deps", None)
    if deps is not None:
        deps.semantic_layer = resolved.layer
        deps.metric_engine = engine


async def refresh_semantic_layer(runtime) -> ResolvedLayer:
    """按当前配置重新装配运行语义层，并就地替换运行时持有物。

    返回新的 ``ResolvedLayer``（含生效版本链，可直接回给管理面）。
    """
    lock = _lock_for(runtime)
    if lock is None:
        return await _do_refresh(runtime)
    async with lock:
        return await _do_refresh(runtime)


async def _do_refresh(runtime) -> ResolvedLayer:
    registry = SemanticRegistry(runtime.db.semantic_published_repo)
    resolved = await resolve_layer(settings=runtime.settings, registry=registry)

    client = getattr(runtime, "starrocks_client", None)
    engine = MetricFlowEngine(resolved.layer, client) if client is not None else None

    apply_resolved(runtime, resolved, engine)

    # 请求级解析服务的基座必须同步换掉并清空租户缓存：否则租户缓存被清空后会拿旧基座
    # 重建，出现"面板显示已更新、实际取数仍是旧定义"这类最难查的失效。
    # 注意 rebase 刻意**不**回调 on_base_changed（调用方就是热更新本身，回写已完成）。
    scope_service = getattr(runtime, "semantic_scope", None)
    if scope_service is not None:
        scope_service.rebase(resolved, engine)

    deps = getattr(runtime, "agent_deps", None)
    logger.info(
        "语义层已热更新：source=%s scope=%s 基座 v%s + overlay %s → 指标 %d 个"
        "（deps %s）",
        resolved.effective_source,
        resolved.scope,
        resolved.base_version,
        resolved.overlay_versions or "无",
        len(resolved.layer.list_metrics()),
        "已同步" if deps is not None else "未装配",
    )
    return resolved
