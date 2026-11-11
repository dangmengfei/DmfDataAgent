"""插件注册表：内置 Skill（Agent 已封装能力）+ 用户自定义 Skill / MCP Server。

- 内置 Skill：只读展示，反映当前 Agent 的实际能力（取数 / 检索 / 归因 / 可视化等）；
- 自定义 Skill / MCP：写入 plugin_store（MySQL，失败退化内存）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dmf_agent.db.stores import PluginStore
from dmf_agent.services.mcp_client import MCPServerConfig, MCPClient
from dmf_agent.skills.archive import remove_package
from dmf_agent.skills.package import SkillPackage

logger = logging.getLogger(__name__)


def _skill(name: str, description: str) -> Dict[str, Any]:
    return {
        "type": "skill",
        "name": name,
        "description": description,
        "endpoint": "",
        "config": "{}",
        "status": "enabled",
        "builtin": True,
    }


# 内置 Skill（对应 Agent 图中已封装的能力，供管理端只读展示）
BUILTIN_SKILLS: List[Dict[str, Any]] = [
    _skill("metric_query", "指标取数：按指标 + 维度 + 时间 + 过滤查询指标数值，支持聚合 / 排序 / top-n"),
    _skill("dimension_query", "维度查询：查询维度成员或维度值本身（如有哪些分行、渠道）"),
    _skill("knowledge_retrieval", "知识检索：BM25 + 向量混合召回知识库文档与业务口径"),
    _skill("cant_lookup", "黑话翻译：口语 / 黑话 → 标准指标与维度术语"),
    _skill("time_extract", "时间解析：相对时间 / 节假日 / 节气 → 绝对时间区间"),
    _skill("metric_semantic", "指标语义层：metricFlow 风格 DSL 构建与语义查询"),
    _skill("attribution", "归因诊断：指标异动的驱动因子分解与多维下钻"),
    _skill("chart_visualization", "图表可视化：查询结果自动生成趋势 / 排名 / 占比图"),
    _skill("data_interpretation", "数据解读：对查询结果做确定性统计解读与结论归纳"),
    _skill("complex_calculation", "复杂计算：多步取数后的加减乘除、再排序 / 求均值等"),
]

BUILTIN_NAMES = {s["name"] for s in BUILTIN_SKILLS}


def skill_package_from_row(row: Dict[str, Any]) -> Optional[SkillPackage]:
    """把一条 skill 记录解成 `SkillPackage`；非声明式包（手动登记 / 空 config）返回 None。"""
    try:
        config = json.loads(row.get("config") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(config, dict) or config.get("format") != "skill.md":
        return None
    pkg = SkillPackage.from_dict(config)
    if not pkg.name:
        pkg.name = str(row.get("name") or "")
    if not pkg.description:
        pkg.description = str(row.get("description") or "")
    return pkg


def skill_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """列表用的轻量视图：含包元信息（版本 / 工具 / 步骤数 / 文件信息），**不含正文**。"""
    item: Dict[str, Any] = {
        "type": row.get("type") or "skill",
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "endpoint": row.get("endpoint") or "",
        "status": row.get("status") or "enabled",
        "builtin": bool(row.get("builtin")),
        "created_at": row.get("created_at"),
    }
    pkg = skill_package_from_row(row)
    if pkg is not None:
        item["is_package"] = True
        item["version"] = pkg.version
        item["tools"] = pkg.tools
        item["step_count"] = len(pkg.steps)
        item["inputs"] = [i.to_dict() for i in pkg.inputs]
        item["triggers"] = pkg.triggers
        item["filename"] = pkg.filename
        item["size"] = pkg.size
        item["sha256"] = pkg.sha256
        item["files"] = list(pkg.files)
        item["file_count"] = len(pkg.files)
        item["has_scripts"] = any(f.get("kind") == "script" for f in pkg.files)
    else:
        item["is_package"] = False
    return item


class PluginRegistry:
    def __init__(self, store: PluginStore, package_base: Optional[Path | str] = None) -> None:
        self._store = store
        # skill 压缩包的落盘根目录（删除插件时据此清理）；空 = 不做磁盘清理
        self._package_base = Path(package_base) if package_base else None

    # ---------- 列表 ----------
    async def list_skills(self) -> List[Dict[str, Any]]:
        custom = await self._store.list("skill")
        return BUILTIN_SKILLS + [skill_summary(r) for r in custom]

    async def list_mcp(self) -> List[Dict[str, Any]]:
        return await self._store.list("mcp")

    async def get_skill(self, name: str) -> Optional[Dict[str, Any]]:
        """按名取单个 skill（内置优先，其次自定义）；不存在返回 None。"""
        name = (name or "").strip()
        for item in BUILTIN_SKILLS:
            if item["name"] == name:
                return item
        return next((r for r in await self._store.list("skill") if r["name"] == name), None)

    async def get_skill_package(self, name: str) -> Optional[SkillPackage]:
        """读取自定义 skill 的声明式包；非包（手动登记 / 内置）返回 None。"""
        row = next((r for r in await self._store.list("skill") if r["name"] == name), None)
        if row is None:
            return None
        pkg = skill_package_from_row(row)
        return self._resolve_package_dir(pkg) if pkg is not None else None

    async def list_skill_packages(self) -> List[SkillPackage]:
        """一次拉取**全部**自定义声明式包（对话路由选技能用）。

        与逐个 `get_skill_package` 相比省掉 N+1 查询：`list_skills` + 每个 name 再拉详情
        会解析两遍 config；这里只拉一次列表、解析一遍。
        """
        packages: List[SkillPackage] = []
        for row in await self._store.list("skill"):
            pkg = skill_package_from_row(row)
            if pkg is not None:
                packages.append(self._resolve_package_dir(pkg))
        return packages

    def _resolve_package_dir(self, pkg: SkillPackage) -> SkillPackage:
        """用**当前配置的落盘根**推导包的磁盘位置。

        落盘位置是确定的 `{package_base}/{name}`，所以不依赖库里存的路径 —— 换机器 /
        换工作目录后旧路径会失效，这里每次读都按当前配置重算。`package_base` 未配置时保留原值。
        """
        if self._package_base is not None and pkg.name:
            pkg.package_dir = str(self._package_base / pkg.name)
        return pkg

    # ---------- 注册 / 删除 ----------
    async def register_skill(self, name: str, description: str = "", config: str = "{}") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "message": "Skill 名称不能为空"}
        if name in BUILTIN_NAMES:
            return {"ok": False, "message": f"「{name}」与内置 Skill 同名，请换一个名称"}
        persisted = await self._store.save("skill", name, description or "", "", config or "{}", "enabled")
        where = "MySQL 已持久化" if persisted else "内存兜底（配置 MySQL 后持久化）"
        return {"ok": True, "message": f"Skill「{name}」已注册（{where}）"}

    async def register_skill_package(self, pkg: SkillPackage) -> Dict[str, Any]:
        """落库一个已解析的 SKILL.md 包（整包存进 `PluginModel.config`，无需新增表/列）。"""
        name = (pkg.name or "").strip()
        if not name:
            return {"ok": False, "message": "Skill 名称不能为空"}
        if name in BUILTIN_NAMES:
            return {"ok": False, "message": f"「{name}」与内置 Skill 同名，请换一个名称"}
        config = json.dumps(pkg.to_dict(), ensure_ascii=False)
        persisted = await self._store.save(
            "skill", name, pkg.description or "", "", config, "enabled"
        )
        where = "MySQL 已持久化" if persisted else "内存兜底（配置 MySQL 后持久化）"
        return {
            "ok": True,
            "message": f"Skill「{name}」已上传（{where}）",
            "item": skill_summary(
                {
                    "type": "skill",
                    "name": name,
                    "description": pkg.description or "",
                    "config": config,
                    "status": "enabled",
                    "builtin": False,
                }
            ),
        }

    async def register_mcp(
        self, name: str, config: Dict[str, Any], description: str = ""
    ) -> Dict[str, Any]:
        """注册 MCP Server（FR14 真实通道）。

        ``config`` 形如：
          - stdio: ``{"transport": "stdio", "command": "python", "args": [...] , "env": {...}}``
          - http:  ``{"transport": "http", "url": "http://host:port/mcp"}``
        """
        name = (name or "").strip()
        if not name:
            return {"ok": False, "message": "MCP 名称不能为空"}
        try:
            cfg = MCPServerConfig.from_config(name, config or {})
            cfg.validate()
        except ValueError as exc:
            return {"ok": False, "message": f"MCP 配置无效：{exc}"}
        endpoint = cfg.url or cfg.command
        config_json = json.dumps(config or {}, ensure_ascii=False)
        persisted = await self._store.save("mcp", name, description or "", endpoint, config_json, "enabled")
        where = "MySQL 已持久化" if persisted else "内存兜底（配置 MySQL 后持久化）"
        return {"ok": True, "message": f"MCP Server「{name}」已注册（{where}）"}

    async def call_mcp_tool(
        self, name: str, tool: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """调用已注册 MCP Server 的某个工具（FR14 真实 invoke 通道）。"""
        row = next((r for r in await self._store.list("mcp") if r["name"] == name), None)
        if row is None:
            return {"ok": False, "message": f"未找到 MCP Server「{name}」"}
        try:
            cfg = MCPServerConfig.from_config(name, json.loads(row.get("config") or "{}"))
            cfg.validate()
        except (ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "message": f"MCP 配置无效：{exc}"}
        try:
            async with MCPClient(cfg) as client:
                result = await client.call_tool(tool, arguments or {})
            return {"ok": True, "server": name, "tool": tool, "result": result}
        except Exception as exc:  # noqa: BLE001 - 调用失败需回传原因，不冒泡
            logger.warning("MCP 调用失败: %s/%s", name, tool, exc_info=True)
            return {"ok": False, "message": f"MCP 调用失败：{exc}"}

    async def probe_mcp(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """用**给定配置**做连接测试（initialize + list_tools），**不落库**。

        供「新增 MCP」弹窗在提交前预检使用（此时条目尚未注册，无行可写）。
        失败不冒泡（与 `call_mcp_tool` 一致），一律以 ``ok=False`` + 可读原因返回。
        """
        try:
            cfg = MCPServerConfig.from_config(name or "mcp", config or {})
            cfg.validate()
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": f"MCP 配置无效：{exc}", "tools": []}

        started = time.time()
        try:
            async with MCPClient(cfg) as client:
                tools = await asyncio.wait_for(client.list_tools(), timeout=cfg.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - 连接失败需回传原因，不冒泡
            elapsed = int((time.time() - started) * 1000)
            logger.warning("MCP 连接测试失败: %s", name, exc_info=True)
            return {"ok": False, "message": f"连接失败：{exc}", "elapsed_ms": elapsed, "tools": []}

        elapsed = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "message": f"连接成功，发现 {len(tools)} 个工具",
            "elapsed_ms": elapsed,
            "tools": tools,
        }

    async def test_mcp(self, name: str) -> Dict[str, Any]:
        """连接测试已注册的 MCP：探测后把工具清单与结果落库。

        失败不冒泡（与 `call_mcp_tool` 一致），但**会把失败也记进 `config.last_test`**，
        便于前端展示"上次测试时间 / 原因"，而不是只看到一次性的报错。
        """
        row = next((r for r in await self._store.list("mcp") if r["name"] == name), None)
        if row is None:
            return {"ok": False, "message": f"未找到 MCP Server「{name}」", "tools": []}
        try:
            config = json.loads(row.get("config") or "{}")
        except json.JSONDecodeError:
            config = {}

        result = await self.probe_mcp(name, config if isinstance(config, dict) else {})
        await self._persist_mcp_test(
            row,
            ok=bool(result.get("ok")),
            elapsed_ms=int(result.get("elapsed_ms") or 0),
            tools=result.get("tools") or [],
            error="" if result.get("ok") else str(result.get("message") or ""),
        )
        return result

    async def _persist_mcp_test(
        self,
        row: Dict[str, Any],
        *,
        ok: bool,
        elapsed_ms: int,
        tools: List[Dict[str, Any]],
        error: str,
    ) -> None:
        """把连接测试结果回写 `config`（工具清单 + last_test）；写失败只告警不阻断。"""
        try:
            config = json.loads(row.get("config") or "{}")
        except json.JSONDecodeError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        if ok:
            config["tools"] = tools
        config["last_test"] = {
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "at": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "tool_count": len(tools),
        }
        try:
            await self._store.save(
                "mcp",
                row["name"],
                row.get("description") or "",
                row.get("endpoint") or "",
                json.dumps(config, ensure_ascii=False),
                row.get("status") or "enabled",
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响测试结论
            logger.warning("MCP 测试结果回写失败: %s", row.get("name"), exc_info=True)

    async def delete(self, plugin_type: str, name: str) -> Dict[str, Any]:
        if plugin_type not in ("skill", "mcp"):
            return {"ok": False, "message": "plugin_type 需为 skill 或 mcp"}
        if plugin_type == "skill" and name in BUILTIN_NAMES:
            return {"ok": False, "message": "内置 Skill 不可删除"}
        deleted = await self._store.delete(plugin_type, name)
        if not deleted:
            return {"ok": False, "message": "删除失败：条目不存在"}
        if plugin_type == "skill" and self._package_base is not None:
            # 落盘包随注册项一并清理（路径固定在 package_base 内，见 archive.remove_package）
            remove_package(self._package_base, name)
        return {"ok": True, "message": "已删除"}
