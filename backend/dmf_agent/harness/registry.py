"""Harness 工具注册表（PRD FR15：工具白名单 + 参数 Schema 强校验 + 输出 Schema 声明）。

主图当前没有自由 ReAct 工具调用，但 `execute_query` / `diagnose_data` / `retrieve_knowledge`
等节点可视为"受约束工具"。ToolRegistry 显式登记每个节点动作、输入 Schema、所需权限，
以及（FR15 输出约束）期望输出的 `output_schema`，Harness 在调用前做白名单/参数/权限校验，
调用成功后按 `output_schema` 强制解析结果（失败重试或交由上层降级）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    required_permissions: List[str] = field(default_factory=list)
    # 是否需要数据权限（行级/列级/指标级）审计，即使当前不拦截
    requires_data_permission: bool = False
    # FR15 输出约束：声明该动作/LLM 调用期望输出的 pydantic Schema；
    # 为 None 时不强制。Harness 在 invoke 成功返回前按此 Schema 解析，
    # 失败则按 output_retries 重试 fn，耗尽则抛 HarnessViolationError 交由上层降级。
    output_schema: Optional[Type[BaseModel]] = None
    output_retries: int = 0


class ToolRegistry:
    """受约束工具/动作注册表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: Optional[Dict[str, Any]] = None,
        required_permissions: Optional[List[str]] = None,
        requires_data_permission: bool = False,
        output_schema: Optional[Type[BaseModel]] = None,
        output_retries: int = 0,
    ) -> "ToolRegistry":
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema or {},
            required_permissions=required_permissions or [],
            requires_data_permission=requires_data_permission,
            output_schema=output_schema,
            output_retries=output_retries,
        )
        return self

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def is_allowed(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        """已注册（即白名单内）的动作名。"""
        return list(self._tools.keys())

    def list_tools(self, user_permissions: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """按用户权限动态收敛的工具列表（用于喂给 LLM 或管理面）。"""
        user_permissions = user_permissions or []
        result: List[Dict[str, Any]] = []
        for spec in self._tools.values():
            if all(p in user_permissions for p in spec.required_permissions):
                result.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "input_schema": spec.input_schema,
                        "required_permissions": spec.required_permissions,
                        "output_schema": getattr(spec.output_schema, "__name__", None),
                    }
                )
        return result

    def validate_arguments(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """按 JSON Schema 强校验参数。返回 {"ok": bool, "error": str}。"""
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"工具/动作 '{name}' 未在白名单注册"}
        schema = spec.input_schema or {}
        required = set(schema.get("required") or [])
        props = schema.get("properties") or {}

        missing = [k for k in required if k not in arguments]
        if missing:
            return {"ok": False, "error": f"缺少必填参数: {missing}"}

        # 仅做类型级基础校验（不引入 jsonschema 外部依赖）
        errors: List[str] = []
        for key, value in arguments.items():
            prop = props.get(key)
            if not prop:
                continue
            ptype = prop.get("type")
            if ptype == "string" and not isinstance(value, str):
                errors.append(f"参数 {key} 应为字符串")
            elif ptype == "integer" and not isinstance(value, int):
                errors.append(f"参数 {key} 应为整数")
            elif ptype == "number" and not isinstance(value, (int, float)):
                errors.append(f"参数 {key} 应为数值")
            elif ptype == "boolean" and not isinstance(value, bool):
                errors.append(f"参数 {key} 应为布尔")
            elif ptype == "array" and not isinstance(value, list):
                errors.append(f"参数 {key} 应为数组")
            elif ptype == "object" and not isinstance(value, dict):
                errors.append(f"参数 {key} 应为对象")
            # 枚举校验
            enum_values = prop.get("enum")
            if enum_values is not None and value not in enum_values:
                errors.append(f"参数 {key} 值 {value} 不在允许列表 {enum_values} 中")
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        return {"ok": True, "error": ""}

    def validate_output(self, name: str, result: Any) -> Dict[str, Any]:
        """FR15 输出约束：按声明 output_schema 校验结果。返回 {"ok": bool, "error": str}。"""
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"工具/动作 '{name}' 未在白名单注册"}
        if spec.output_schema is None:
            return {"ok": True, "error": ""}
        try:
            spec.output_schema.model_validate(result)
            return {"ok": True, "error": ""}
        except Exception as exc:  # noqa: BLE001 - 任何校验异常都视为不符合 Schema
            return {"ok": False, "error": f"输出不符合 Schema：{exc}"}

    def coerce_output(self, name: str, result: Any) -> Any:
        """FR15 输出约束：将结果按声明 output_schema 规范化为 pydantic 实例（无声明则原样返回）。"""
        spec = self._tools.get(name)
        if spec is None or spec.output_schema is None:
            return result
        return spec.output_schema.model_validate(result)


def build_default_tool_registry() -> ToolRegistry:
    """主图节点动作注册。"""
    registry = ToolRegistry()
    registry.register(
        "execute_query",
        "执行语义查询：按 DSL 编译 SQL 并在 StarRocks/MySQL 取数",
        input_schema={
            "type": "object",
            "required": ["dsl"],
            "properties": {
                "dsl": {
                    "type": "object",
                    "description": "语义查询 DSL（metrics/dimensions/filters/time_range/granularity）",
                },
            },
        },
        required_permissions=["semantic:query"],
        requires_data_permission=True,
    )
    registry.register(
        "analyze_data",
        "对 query_result 做描述性统计与高级分析",
        input_schema={
            "type": "object",
            "required": ["query_result"],
            "properties": {
                "query_result": {"type": "object"},
            },
        },
        required_permissions=["semantic:query"],
    )
    registry.register(
        "diagnose_data",
        "对指标波动做归因下钻（维度贡献度 / 异动检测）",
        input_schema={
            "type": "object",
            "required": ["dsl"],
            "properties": {
                "dsl": {"type": "object"},
            },
        },
        required_permissions=["semantic:query"],
        requires_data_permission=True,
    )
    registry.register(
        "retrieve_knowledge",
        "从知识库召回口径/文档片段",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
            },
        },
        # 与 RBAC PERM_KNOWLEDGE（"knowledge"）对齐，不是 "knowledge:read"
        required_permissions=["knowledge"],
    )
    registry.register(
        "synthesize_answer",
        "根据运行结果生成自然语言答复",
        input_schema={
            "type": "object",
            "required": ["state"],
            "properties": {"state": {"type": "object"}},
        },
        required_permissions=[],
    )
    registry.register(
        "mcp_tool",
        "调用已注册的 MCP Server 工具（FR14 真实通道）",
        input_schema={
            "type": "object",
            "required": ["server", "tool"],
            "properties": {
                "server": {"type": "string", "description": "已注册的 MCP Server 名称"},
                "tool": {"type": "string", "description": "工具名"},
                "arguments": {"type": "object", "description": "工具入参（JSON 对象）"},
            },
        },
        # 与 RBAC PERM_PLUGIN（"plugin"）对齐：有插件可见/调用权限即可 invoke
        required_permissions=["plugin"],
    )
    registry.register(
        "run_skill_script",
        "执行 skill 包自带的脚本（仅 .py/.sh；无 shell、剥离环境变量、限 CPU/内存/输出/超时）",
        input_schema={
            "type": "object",
            "required": ["script"],
            "properties": {
                "script": {"type": "string", "description": "包内脚本的相对路径，如 scripts/render.py"},
                "args": {"type": "array", "description": "命令行参数（字符串数组）"},
            },
        },
        # 与 mcp_tool 一致：有插件权限即可执行包内脚本
        required_permissions=["plugin"],
    )
    return registry
