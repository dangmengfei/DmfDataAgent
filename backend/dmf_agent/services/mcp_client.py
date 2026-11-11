"""MCP 真实 invoke 通道（FR14）。

基于官方 MCP Python SDK 封装：支持 stdio（本地进程）与 streamable-http 两种传输，
提供 ``list_tools`` / ``call_tool``，并把工具结果规范化为字符串供 Agent 消费。

这是 PRD FR14 的「真实接入与 invoke 通道」：此前 ``PluginRegistry`` 仅持久化 endpoint
配置、无任何实际调用；现经 MCP SDK 建立会话、调用工具，并可接入 Harness 的白名单 /
权限 / 预算治理（见 ``harness/registry.py`` 的 ``mcp_tool`` 动作）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """一个 MCP Server 的连接配置。"""

    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    timeout_seconds: float = 30.0

    @classmethod
    def from_config(cls, name: str, config: Dict[str, Any]) -> "MCPServerConfig":
        config = config or {}
        transport = str(config.get("transport") or "stdio").lower()
        if transport not in ("stdio", "http"):
            raise ValueError(f"不支持的 MCP transport: {transport}")
        return cls(
            name=name,
            transport=transport,
            command=str(config.get("command") or ""),
            args=list(config.get("args") or []),
            env={k: str(v) for k, v in (config.get("env") or {}).items()},
            url=str(config.get("url") or ""),
            timeout_seconds=float(config.get("timeout_seconds") or 30.0),
        )

    def validate(self) -> None:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio 传输需要 command")
        else:
            if not self.url:
                raise ValueError("http 传输需要 url")


def normalize_tool_result(result: Any) -> str:
    """将 MCP ``CallToolResult`` 规范化为字符串。

    优先拼接各 content 片段：``TextContent`` -> ``.text``；``ImageContent`` -> 标注；
    ``EmbeddedResource`` -> 标注。再补结构化内容（``structuredContent``，JSON）。
    """
    if result is None:
        return ""
    contents = getattr(result, "content", None)
    if contents is None:
        contents = result if isinstance(result, list) else [result]

    parts: List[str] = []
    for item in contents:
        if isinstance(item, TextContent):
            parts.append(item.text or "")
        elif isinstance(item, ImageContent):
            mime = getattr(item, "mimeType", "") or "image"
            parts.append(f"[image:{mime}]")
        elif isinstance(item, EmbeddedResource):
            mime = getattr(getattr(item, "resource", None), "mimeType", "") or "resource"
            parts.append(f"[resource:{mime}]")
        elif isinstance(item, dict):
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", ""))
            elif t == "image":
                parts.append(f"[image:{item.get('mimeType', 'image')}]")
            elif t == "resource":
                parts.append(f"[resource:{item.get('mimeType', 'resource')}]")
            else:
                parts.append(str(item))
        else:
            parts.append(str(item))

    text = "\n".join(p for p in parts if p)
    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            text = (text + "\n" if text else "") + json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return text


class MCPClient:
    """MCP 客户端：建立会话、列出/调用工具。建议用 ``async with`` 管理生命周期。"""

    def __init__(self, config: MCPServerConfig) -> None:
        config.validate()
        self.config = config
        self._session: Optional[ClientSession] = None
        self._transport_ctx = None
        self._session_ctx = None

    @classmethod
    def for_session(cls, session: ClientSession) -> "MCPClient":
        """测试用：直接包一个已初始化的会话（调用方负责生命周期）。"""
        inst = cls.__new__(cls)
        inst.config = MCPServerConfig(name="test", transport="stdio")
        inst._session = session
        inst._transport_ctx = None
        inst._session_ctx = None
        return inst

    async def _enter_stdio(self) -> None:
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env={**os.environ, **self.config.env} if self.config.env else None,
        )
        self._transport_ctx = stdio_client(params)
        read, write = await self._transport_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    async def _enter_http(self) -> None:
        self._transport_ctx = streamablehttp_client(self.config.url)
        read, write, _ = await self._transport_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    async def __aenter__(self) -> "MCPClient":
        if self.config.transport == "stdio":
            await self._enter_stdio()
        else:
            await self._enter_http()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        for ctx in (self._session_ctx, self._transport_ctx):
            if ctx is None:
                continue
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - 关闭异常不应掩盖主流程
                logger.warning("MCP 会话/传输关闭异常", exc_info=True)
        self._session_ctx = self._transport_ctx = self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCPClient 未连接，请使用 async with 或 for_session 注入会话")
        return self._session

    async def list_tools(self) -> List[Dict[str, Any]]:
        session = self._require_session()
        result = await asyncio.wait_for(session.list_tools(), timeout=self.config.timeout_seconds)
        return [
            {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", None) or {},
            }
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        session = self._require_session()
        result: CallToolResult = await asyncio.wait_for(
            session.call_tool(name, arguments or {}), timeout=self.config.timeout_seconds
        )
        return normalize_tool_result(result)


async def call_mcp_server(
    config: MCPServerConfig, tool: str, arguments: Dict[str, Any]
) -> str:
    """一次性便捷调用：建立连接 -> 调工具 -> 关闭。"""
    async with MCPClient(config) as client:
        return await client.call_tool(tool, arguments)
