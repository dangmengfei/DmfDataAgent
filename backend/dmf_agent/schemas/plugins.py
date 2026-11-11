"""插件管理 API 模型（Skill / MCP Server）。"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class SkillAdd(BaseModel):
    name: str = Field(..., min_length=1, description="Skill 名称")
    description: str = Field("", description="描述")
    config: str = Field("{}", description="配置 JSON 字符串")


class SkillRun(BaseModel):
    """执行一个已注册 skill（沙箱内受治理运行）。"""

    inputs: Dict[str, Any] = Field(default_factory=dict, description="skill 输入（按 frontmatter 的 inputs 声明）")


class McpAdd(BaseModel):
    name: str = Field(..., min_length=1, description="MCP Server 名称")
    description: str = Field("", description="描述")
    transport: str = Field("stdio", description="传输方式：stdio | http")
    command: str = Field("", description="stdio 传输：启动命令（如 python）")
    args: List[str] = Field(default_factory=list, description="stdio 传输：命令参数")
    url: str = Field("", description="http 传输：streamable-http 地址")
    env: Dict[str, str] = Field(default_factory=dict, description="stdio 传输：额外环境变量")
    timeout_seconds: float = Field(30.0, description="调用超时（秒）")


class McpCall(BaseModel):
    tool: str = Field(..., min_length=1, description="工具名")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具入参")
