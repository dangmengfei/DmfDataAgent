"""FR14 MCP 真实 invoke 通道测试。

覆盖：
1. ``MCPServerConfig.from_config`` / ``validate``（stdio / http 校验）。
2. ``normalize_tool_result``（TextContent / ImageContent / EmbeddedResource / dict / structuredContent）。
3. ``MCPClient`` 经真实 FastMCP stdio 子进程做 list_tools + call_tool 往返。
4. ``PluginRegistry.call_mcp_tool`` 经内存存储注册后真实调用。
5. ``POST /plugins/mcp`` 注册 + ``POST /plugins/mcp/{name}/call`` 接口真实调用。
6. ``MCPClient.for_session`` 用 mock 会话验证 list/call 路径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from dmf_agent.db.factory import build_database
from dmf_agent.db.stores import PluginStore
from dmf_agent.services.mcp_client import (
    MCPServerConfig,
    MCPClient,
    call_mcp_server,
    normalize_tool_result,
)
from dmf_agent.services.plugins import PluginRegistry
from dmf_agent.services.runtime import AppRuntime
from dmf_agent.config.settings import Settings
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)


# ---------------------------------------------------------------------------
# 工具：写一份最小 FastMCP stdio 服务端脚本
# ---------------------------------------------------------------------------
SERVER_SRC = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("dmf-test-echo")

@mcp.tool()
def echo(text: str) -> str:
    return f"echo:{text}"

@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b

if __name__ == "__main__":
    mcp.run()
'''


@pytest.fixture
def server_script(tmp_path: Path) -> str:
    p = tmp_path / "mcp_server.py"
    p.write_text(SERVER_SRC, encoding="utf-8")
    return str(p)


@pytest.fixture
def stdio_config(server_script: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="demo",
        transport="stdio",
        command=sys.executable,
        args=[server_script],
        timeout_seconds=30.0,
    )


# ---------------------------------------------------------------------------
# 1. MCPServerConfig.from_config / validate
# ---------------------------------------------------------------------------
def test_from_config_defaults_and_transport():
    cfg = MCPServerConfig.from_config("svc", {})
    assert cfg.name == "svc"
    assert cfg.transport == "stdio"
    assert cfg.command == ""
    assert cfg.args == []
    assert cfg.timeout_seconds == 30.0


def test_from_config_http():
    cfg = MCPServerConfig.from_config(
        "svc",
        {"transport": "HTTP", "url": "http://host:9000/mcp", "timeout_seconds": 5},
    )
    assert cfg.transport == "http"
    assert cfg.url == "http://host:9000/mcp"
    assert cfg.timeout_seconds == 5.0


def test_from_config_invalid_transport_raises():
    with pytest.raises(ValueError):
        MCPServerConfig.from_config("svc", {"transport": "grpc"})


def test_validate_stdio_requires_command():
    cfg = MCPServerConfig(name="x", transport="stdio", command="")
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_http_requires_url():
    cfg = MCPServerConfig(name="x", transport="http", url="")
    with pytest.raises(ValueError):
        cfg.validate()


# ---------------------------------------------------------------------------
# 2. normalize_tool_result
# ---------------------------------------------------------------------------
def test_normalize_text_content():
    res = CallToolResult(content=[TextContent(type="text", text="你好")], structuredContent=None)
    assert normalize_tool_result(res) == "你好"


def test_normalize_image_and_resource():
    res = CallToolResult(
        content=[
            ImageContent(type="image", data="aaa", mimeType="image/png"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="file://x", mimeType="application/json", text="{}"
                ),
            ),
        ],
        structuredContent=None,
    )
    out = normalize_tool_result(res)
    assert "[image:image/png]" in out
    assert "[resource:application/json]" in out


def test_normalize_dict_and_structured():
    res = SimpleNamespace(
        content=[{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
        structuredContent={"k": "v"},
    )
    out = normalize_tool_result(res)
    assert "part1" in out and "part2" in out
    assert '"k": "v"' in out or '"k":"v"' in out


def test_normalize_none():
    assert normalize_tool_result(None) == ""


# ---------------------------------------------------------------------------
# 3. MCPClient 真实 FastMCP stdio 往返
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mcp_client_stdio_roundtrip(stdio_config: MCPServerConfig):
    async with MCPClient(stdio_config) as client:
        tools = await client.list_tools()
        names = {t["name"] for t in tools}
        assert "echo" in names and "add" in names

        echo_out = await client.call_tool("echo", {"text": "hello"})
        assert "echo:hello" in echo_out

        add_out = await client.call_tool("add", {"a": 2, "b": 3})
        # FastMCP 对 int 返回会序列化为文本或结构化内容，重点验证无异常且含结果
        assert "5" in add_out


@pytest.mark.asyncio
async def test_call_mcp_server_convenience(stdio_config: MCPServerConfig):
    out = await call_mcp_server(stdio_config, "echo", {"text": "conv"})
    assert "echo:conv" in out


# ---------------------------------------------------------------------------
# 4. PluginRegistry.call_mcp_tool（内存存储）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_registry_call_mcp_tool(server_script: str):
    reg = PluginRegistry(PluginStore(None))
    reg_result = await reg.register_mcp(
        "demo",
        {"transport": "stdio", "command": sys.executable, "args": [server_script]},
        "demo server",
    )
    assert reg_result["ok"] is True

    listed = await reg.list_mcp()
    assert any(r["name"] == "demo" for r in listed)

    out = await reg.call_mcp_tool("demo", "echo", {"text": "registry"})
    assert out["ok"] is True
    assert out["server"] == "demo"
    assert out["tool"] == "echo"
    assert "echo:registry" in out["result"]


@pytest.mark.asyncio
async def test_registry_call_missing_server():
    reg = PluginRegistry(PluginStore(None))
    out = await reg.call_mcp_tool("nope", "echo", {})
    assert out["ok"] is False
    assert "未找到" in out["message"]


# ---------------------------------------------------------------------------
# 5. API 端点：注册 + 调用
# ---------------------------------------------------------------------------
def _build_client(server_script: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dmf_agent.api.routers import plugins as plugins_router

    settings = Settings()
    settings.db.enabled = False
    settings.auth.enabled = False
    db = build_database(settings)
    app = FastAPI()
    app.include_router(plugins_router.router, prefix="/api/v1")
    app.state.runtime = AppRuntime(
        settings=settings,
        db=db,
        graph=None,
        chat_service=None,
        retriever=None,
        rag_backend="bm25",
        plugin_registry=PluginRegistry(db.plugin_store),
    )
    return TestClient(app), db


def test_api_mcp_register_and_call(server_script: str):
    client, _ = _build_client(server_script)

    # 注册
    resp = client.post(
        "/api/v1/plugins/mcp",
        json={
            "name": "demo",
            "transport": "stdio",
            "command": sys.executable,
            "args": [server_script],
            "description": "demo",
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True

    # 调用
    resp = client.post(
        "/api/v1/plugins/mcp/demo/call",
        json={"tool": "echo", "arguments": {"text": "api"}},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "echo:api" in body["result"]


def test_api_mcp_call_unknown_server(server_script: str):
    client, _ = _build_client(server_script)
    resp = client.post(
        "/api/v1/plugins/mcp/ghost/call",
        json={"tool": "echo", "arguments": {}},
    )
    body = resp.json()
    assert body["ok"] is False
    assert "未找到" in body["message"]


# ---------------------------------------------------------------------------
# 5b. 连接测试：initialize + list_tools，工具清单落库
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_registry_test_mcp_unknown_server():
    reg = PluginRegistry(PluginStore(None))
    out = await reg.test_mcp("ghost")
    assert out["ok"] is False
    assert "未找到" in out["message"]


@pytest.mark.asyncio
async def test_registry_test_mcp_records_failure(server_script: str):
    """连不上的 server：ok=False、带原因，且失败也记进 config.last_test。"""
    reg = PluginRegistry(PluginStore(None))
    await reg.register_mcp(
        "broken",
        {"transport": "stdio", "command": "/nonexistent/mcp-binary-xyz", "timeout_seconds": 5},
    )
    out = await reg.test_mcp("broken")
    assert out["ok"] is False
    assert "连接失败" in out["message"]
    assert out["tools"] == []

    row = next(r for r in await reg.list_mcp() if r["name"] == "broken")
    last_test = json.loads(row["config"])["last_test"]
    assert last_test["ok"] is False
    assert last_test["error"]
    assert last_test["at"]


def test_api_mcp_test_ok_and_persists_tools(server_script: str):
    client, _ = _build_client(server_script)
    client.post(
        "/api/v1/plugins/mcp",
        json={
            "name": "demo",
            "transport": "stdio",
            "command": sys.executable,
            "args": [server_script],
            "description": "demo",
        },
    )

    resp = client.post("/api/v1/plugins/mcp/demo/test")
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True, body
    names = {t["name"] for t in body["tools"]}
    assert {"echo", "add"} <= names
    assert body["elapsed_ms"] >= 0
    assert "发现 2 个工具" in body["message"]

    # 工具清单与 last_test 已落库，列表接口即可见（无需再连一次）
    row = next(r for r in client.get("/api/v1/plugins/mcp").json()["items"] if r["name"] == "demo")
    config = json.loads(row["config"])
    assert {t["name"] for t in config["tools"]} == {"echo", "add"}
    assert config["tools"][0]["input_schema"] is not None
    assert config["last_test"]["ok"] is True
    assert config["last_test"]["tool_count"] == 2


def test_api_mcp_test_unreachable_returns_reason(server_script: str):
    client, _ = _build_client(server_script)
    client.post(
        "/api/v1/plugins/mcp",
        json={
            "name": "broken",
            "transport": "stdio",
            "command": "/nonexistent/mcp-binary-xyz",
            "timeout_seconds": 5,
        },
    )
    body = client.post("/api/v1/plugins/mcp/broken/test").json()
    assert body["ok"] is False
    assert "连接失败" in body["message"]


def test_api_mcp_test_unknown_server(server_script: str):
    client, _ = _build_client(server_script)
    body = client.post("/api/v1/plugins/mcp/ghost/test").json()
    assert body["ok"] is False
    assert "未找到" in body["message"]


# ---------------------------------------------------------------------------
# 5c. 新增弹窗预检：用未保存的临时配置试连，不落库
# ---------------------------------------------------------------------------
def test_api_mcp_probe_ok_without_persisting(server_script: str):
    """未注册的配置也能试连；成功后**不产生任何条目**（列表仍为空）。"""
    client, _ = _build_client(server_script)
    body = client.post(
        "/api/v1/plugins/mcp/test",
        json={
            "name": "preview",
            "transport": "stdio",
            "command": sys.executable,
            "args": [server_script],
        },
    ).json()
    assert body["ok"] is True, body
    assert {t["name"] for t in body["tools"]} == {"echo", "add"}
    assert client.get("/api/v1/plugins/mcp").json()["items"] == []


def test_api_mcp_probe_invalid_config_returns_reason():
    """配置不合法（http 缺 url）：ok=False 且原因可读，不抛 500。"""
    client, _ = _build_client("")
    body = client.post(
        "/api/v1/plugins/mcp/test",
        json={"name": "bad", "transport": "http", "url": ""},
    ).json()
    assert body["ok"] is False
    assert "MCP 配置无效" in body["message"]
    assert body["tools"] == []


def test_api_mcp_probe_unreachable_returns_reason(server_script: str):
    client, _ = _build_client(server_script)
    body = client.post(
        "/api/v1/plugins/mcp/test",
        json={
            "name": "broken",
            "transport": "stdio",
            "command": "/nonexistent/mcp-binary-xyz",
            "timeout_seconds": 5,
        },
    ).json()
    assert body["ok"] is False
    assert "连接失败" in body["message"]



# ---------------------------------------------------------------------------
# 6. MCPClient.for_session（mock 会话）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mcp_client_for_session():
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[SimpleNamespace(name="t1", description="d", inputSchema={})]
    )
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="ok")], structuredContent=None
    )
    client = MCPClient.for_session(session)
    tools = await client.list_tools()
    assert tools[0]["name"] == "t1"
    assert await client.call_tool("t1", {}) == "ok"
