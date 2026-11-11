"""插件管理接口：内置 Skill 展示 + 自定义 Skill 注册/上传/执行 + MCP Server 注册/连接测试。

- Skill：支持手动登记（名称 + 描述）与**上传 SKILL.md 声明式包**（`skills/package.py` 解析）；
  执行走 `skills/executor.SkillExecutor` 的沙箱（Harness 白名单 / 权限 / 预算 / 超时 / 审计）。
- MCP：注册后可做**连接测试**（initialize + list_tools），工具清单落库供前端展示。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_PLUGIN, PERM_PLUGIN_WRITE
from dmf_agent.harness import HarnessContext, HarnessState
from dmf_agent.harness.sandbox import get_default_sandbox
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.plugins import McpAdd, McpCall, SkillAdd, SkillRun
from dmf_agent.services.plugins import skill_summary
from dmf_agent.services.runtime import AppRuntime
from dmf_agent.skills import (
    ALLOWED_SUFFIXES,
    ArchiveError,
    SkillParseError,
    commit,
    discard,
    extract_skill_zip,
    kind_of,
    parse_skill_package_dir,
    read_package_file,
    remove_package,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]


def _harness_context(current: CurrentUser) -> HarnessContext:
    """把当前身份装进 Harness 上下文：skill 沙箱据此做权限点校验。"""
    return HarnessContext(
        session_id="",
        user_id=current.id,
        tenant_id=current.tenant_id,
        user_permissions=list(current.permissions or []),
    )


@router.get("/plugins/skills", summary="Skill 列表（内置 + 自定义）", dependencies=_guard(PERM_PLUGIN))
async def list_skills(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    items = await runtime.plugin_registry.list_skills()
    return {"ok": True, "items": items, "count": len(items)}


@router.get("/plugins/mcp", summary="MCP Server 列表", dependencies=_guard(PERM_PLUGIN))
async def list_mcp(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    items = await runtime.plugin_registry.list_mcp()
    return {"ok": True, "items": items, "count": len(items)}


@router.post("/plugins/skills", summary="注册自定义 Skill（手动登记）", dependencies=_guard(PERM_PLUGIN_WRITE))
async def add_skill(
    body: SkillAdd, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.plugin_registry.register_skill(body.name, body.description, body.config)


@router.post(
    "/plugins/skills/upload",
    summary="上传 Skill 整包（ZIP：SKILL.md + 脚本 / 文档等）",
    dependencies=_guard(PERM_PLUGIN_WRITE),
)
async def upload_skill(
    file: UploadFile,
    description: str = Form(""),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """上传并落库一个 skill 整包。

    闸口（任一不过即 400，**不落盘、不落库**）：后缀仅 `.zip` → 压缩包大小上限 →
    安全解压（zip-slip / 符号链接 / 加密 / 限额 / zip 炸弹，见 `skills/archive.py`）→
    包内**逐个文本文件**过沙箱内容扫描 → 包解析（须含 SKILL.md）→
    工具名必须在 Harness 白名单内 → 原子落盘 → 落库。
    """
    filename = file.filename or "skill.zip"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{suffix or '（无后缀）'}，"
            f"仅支持 {'/'.join(ALLOWED_SUFFIXES)}（skill 需以压缩包整包上传）",
        )

    raw = await file.read()
    max_bytes = runtime.settings.harness.skill_max_file_bytes
    if max_bytes > 0 and len(raw) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"压缩包过大：{len(raw)} 字节，上限 {max_bytes} 字节",
        )

    base = Path(runtime.settings.skill.package_dir)
    try:
        extracted = extract_skill_zip(
            raw,
            base,
            max_bytes=runtime.settings.harness.skill_max_package_bytes,
            max_files=runtime.settings.harness.skill_max_package_files,
            max_file_bytes=max_bytes,
        )
    except ArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _scan_package(extracted.root)
        try:
            pkg = parse_skill_package_dir(extracted.root, filename=filename)
        except SkillParseError as exc:
            raise HTTPException(status_code=400, detail=f"Skill 包解析失败：{exc}") from exc
        pkg.files = list(extracted.files)

        allowed = runtime.harness.registry.names() if runtime.harness is not None else []
        unknown = [t for t in pkg.step_tools() if t not in allowed]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Skill 使用了未在 Harness 白名单注册的工具：{'、'.join(unknown)}"
                f"（可用：{'、'.join(allowed) or '无'}）",
            )

        if description.strip():
            pkg.description = description.strip()

        final = commit(extracted, base, pkg.name)
        pkg.package_dir = str(final)
    except HTTPException:
        discard(extracted)
        raise
    except Exception as exc:  # noqa: BLE001 - 任何意外都清理临时目录，避免留垃圾
        discard(extracted)
        raise HTTPException(status_code=400, detail=f"Skill 包处理失败：{exc}") from exc

    result = await runtime.plugin_registry.register_skill_package(pkg)
    if not result.get("ok"):
        remove_package(base, pkg.name)
        raise HTTPException(status_code=400, detail=result.get("message") or "上传失败")
    return result


def _scan_package(root: Path) -> None:
    """沙箱闸口：扫描包内全部文本文件，命中危险模式即拒绝。

    分级扫描（避免误伤）：**可执行脚本**过全量文本层（代码执行 / 外联 / 越权 / 注入）；
    **SKILL.md 与说明文档**只过注入类规则——文档里出现 `subprocess.run` / `curl` 示例
    属正常说明，不应因此拒绝整包。
    """
    sandbox = get_default_sandbox()
    if sandbox is None:
        return
    hits: Dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        _, text = read_package_file(root, rel)
        if not text:
            continue
        found = sandbox.scan_text(text) if kind_of(rel) == "script" else sandbox.scan_doc(text)
        if found:
            hits[rel] = sorted({str(h.get("matched")) for h in found})
    if hits:
        detail = "；".join(f"{name} 命中 {'、'.join(patterns)}" for name, patterns in hits.items())
        raise HTTPException(status_code=400, detail=f"Skill 包内容命中安全沙箱规则，已拒绝：{detail}")


@router.get("/plugins/skills/{name}", summary="Skill 详情（含声明式包内容）", dependencies=_guard(PERM_PLUGIN))
async def get_skill(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    row = await runtime.plugin_registry.get_skill(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 Skill「{name}」")
    pkg = await runtime.plugin_registry.get_skill_package(name)
    return {
        "ok": True,
        "item": skill_summary(row) if not row.get("builtin") else row,
        "package": pkg.to_dict() if pkg is not None else None,
    }


@router.post(
    "/plugins/skills/{name}/run",
    summary="执行 Skill（沙箱内受治理运行）",
    dependencies=_guard(PERM_PLUGIN),
)
async def run_skill(
    name: str,
    body: SkillRun,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    executor = runtime.skill_executor
    if executor is None:
        raise HTTPException(status_code=503, detail="Skill 执行器未装配（请检查应用启动日志）")

    pkg = await runtime.plugin_registry.get_skill_package(name)
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到可执行的 Skill「{name}」（内置 Skill 由主图直接编排，或在插件面板手动登记的项目未声明 steps）",
        )

    result = await executor.run(pkg, body.inputs, ctx=_harness_context(current), state=HarnessState())
    return result


@router.post("/plugins/mcp", summary="注册 MCP Server", dependencies=_guard(PERM_PLUGIN_WRITE))
async def add_mcp(body: McpAdd, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    config = {
        "transport": body.transport,
        "command": body.command,
        "args": body.args,
        "url": body.url,
        "env": body.env,
        "timeout_seconds": body.timeout_seconds,
    }
    return await runtime.plugin_registry.register_mcp(body.name, config, body.description)


@router.post(
    "/plugins/mcp/test",
    summary="MCP 连接测试（未保存的临时配置，不落库）",
    dependencies=_guard(PERM_PLUGIN_WRITE),
)
async def probe_mcp(body: McpAdd, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """「新增 MCP」弹窗的预检：用表单当前配置试连，通过后再提交注册。"""
    config = {
        "transport": body.transport,
        "command": body.command,
        "args": body.args,
        "url": body.url,
        "env": body.env,
        "timeout_seconds": body.timeout_seconds,
    }
    return await runtime.plugin_registry.probe_mcp(body.name, config)


@router.post(
    "/plugins/mcp/{name}/test",
    summary="MCP Server 连接测试（initialize + list_tools，工具清单落库）",
    dependencies=_guard(PERM_PLUGIN),
)
async def test_mcp(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return await runtime.plugin_registry.test_mcp(name)


@router.post(
    "/plugins/mcp/{name}/call",
    summary="调用 MCP Server 工具（FR14 真实 invoke 通道）",
    dependencies=_guard(PERM_PLUGIN),
)
async def call_mcp(
    name: str, body: McpCall, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.plugin_registry.call_mcp_tool(name, body.tool, body.arguments)


@router.delete("/plugins/delete", summary="删除插件（skill / mcp）", dependencies=_guard(PERM_PLUGIN_WRITE))
async def delete_plugin(
    plugin_type: str = "", name: str = "", runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.plugin_registry.delete(plugin_type, name)
