"""MCP 工具调用节点（FR14 真实通道的受约束入口）。

通过 Harness 的 ``mcp_tool`` 动作走白名单 / 权限 / 预算校验后，再调用
``PluginRegistry.call_mcp_tool``。当前 Agent 主图没有自由 ReAct 工具循环，
因此本节点作为"受治理的 MCP 调用原语"存在，供后续编排（如 Sub-Agent 工具路由）复用。
对外真实 invoke 通道已由 ``api/routers/plugins.py::call_mcp`` 暴露。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState
from dmf_agent.harness import HarnessContext, HarnessState

logger = logging.getLogger(__name__)


def build_mcp_tool_node(deps: AgentDeps):
    async def mcp_tool(state: AgentState) -> Dict[str, Any]:
        server = state.get("mcp_server") or ""
        tool = state.get("mcp_tool_name") or ""
        arguments = state.get("mcp_arguments") or {}
        if not server or not tool:
            return {"mcp_result": {"ok": False, "message": "缺少 mcp_server / mcp_tool_name"}}

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

        async def _raw() -> Dict[str, Any]:
            registry = getattr(deps, "plugin_registry", None)
            if registry is None:
                return {"ok": False, "message": "plugin_registry 未装配（AgentDeps 未注入）"}
            return await registry.call_mcp_tool(server, tool, arguments)

        if harness is not None:
            result = await harness.invoke(
                "mcp_tool", {"server": server, "tool": tool, "arguments": arguments},
                hctx, harness_state, _raw,
            )
        else:
            result = await _raw()
        return {"mcp_result": result, "harness_state": harness_state.snapshot()}

    return mcp_tool
