"""模型管理接口：对话 LLM 配置 CRUD + 连接测试 + 设为默认。

- 配置入库（llm_configs 表，MySQL / 内存双态），支持 CRUD；
- 连接测试两条：``POST /models/probe``（新增/编辑弹窗预检，不落库）与
  ``POST /models/{name}/test``（已保存条目，结果回写 ``last_test``），
  对齐 ``plugins.py`` 的 MCP 试连惯用法；
- ``POST /models/{name}/default``：标记唯一默认模型，作为运行时生效模型
  （覆盖环境变量 ``DMF_LLM-*``），管理面变更即时生效。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_permission
from dmf_agent.auth.roles import PERM_MODEL, PERM_MODEL_WRITE
from dmf_agent.schemas.models import ModelAdd, ModelProbe, ModelUpdate
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter()


def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]


def _err(result: Dict[str, Any]) -> None:
    """把服务层 {ok, not_found, message} 转成 HTTP 异常（404 / 400）。"""
    if result.get("not_found"):
        raise HTTPException(status_code=404, detail=result.get("message") or "未找到")
    raise HTTPException(status_code=400, detail=result.get("message") or "操作失败")


@router.get("/models", summary="模型列表（api_key 脱敏）", dependencies=_guard(PERM_MODEL))
async def list_models(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return await runtime.model_registry.list()


@router.get("/models/{name}", summary="模型详情（api_key 脱敏）", dependencies=_guard(PERM_MODEL))
async def get_model(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    item = await runtime.model_registry.get(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到模型「{name}」")
    return {"ok": True, "item": item}


@router.post("/models", summary="新增模型配置", dependencies=_guard(PERM_MODEL_WRITE))
async def create_model(body: ModelAdd, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    result = await runtime.model_registry.create(body)
    if not result.get("ok"):
        _err(result)
    return result


@router.put("/models/{name}", summary="更新模型配置（api_key 留空 = 保留原值）", dependencies=_guard(PERM_MODEL_WRITE))
async def update_model(
    name: str, body: ModelUpdate, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    result = await runtime.model_registry.update(name, body)
    if not result.get("ok"):
        _err(result)
    return result


@router.delete("/models/{name}", summary="删除模型配置", dependencies=_guard(PERM_MODEL_WRITE))
async def delete_model(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    result = await runtime.model_registry.delete(name)
    if not result.get("ok"):
        _err(result)
    return result


@router.post(
    "/models/{name}/default",
    summary="设为默认模型（运行时生效）",
    dependencies=_guard(PERM_MODEL_WRITE),
)
async def set_default_model(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    result = await runtime.model_registry.set_default(name)
    if not result.get("ok"):
        _err(result)
    return result


@router.post(
    "/models/probe",
    summary="连接测试（未保存的临时配置，不落库）",
    dependencies=_guard(PERM_MODEL_WRITE),
)
async def probe_model(body: ModelProbe, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """「新增/编辑模型」弹窗的预检：用表单当前配置试连，通过后再提交。"""
    return await runtime.model_registry.probe(body)


@router.post(
    "/models/{name}/test",
    summary="连接测试已保存的模型（结果回写 last_test）",
    dependencies=_guard(PERM_MODEL),
)
async def test_model(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return await runtime.model_registry.test(name)
