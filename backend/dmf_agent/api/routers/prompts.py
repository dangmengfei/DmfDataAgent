"""系统提示词管理接口：模板列表 / 版本历史 / 详情 / 保存新版本 / 版本回溯。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_permission
from dmf_agent.auth.roles import PERM_PROMPT, PERM_PROMPT_WRITE
from dmf_agent.schemas.prompts import (
    PromptAbCompare,
    PromptEval,
    PromptPromote,
    PromptRollback,
    PromptUpdate,
)
from dmf_agent.schemas.eval import PromptEvalByDataset
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()

def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]



@router.get("/prompts/list", summary="提示词模板列表（当前生效版本 + 历史数量）", dependencies=_guard(PERM_PROMPT))
async def list_prompts(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return await runtime.prompt_manager.list()


@router.get("/prompts/history", summary="模板版本历史", dependencies=_guard(PERM_PROMPT))
async def prompt_history(name: str, runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return await runtime.prompt_manager.history(name)


@router.get("/prompts/detail", summary="模板指定版本内容", dependencies=_guard(PERM_PROMPT))
async def prompt_detail(
    name: str, version: str = "", runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    if not version:
        versions = await runtime.prompt_manager.history(name)
        if not versions.get("ok"):
            return versions
        version = versions.get("current", "")
    return await runtime.prompt_manager.detail(name, version)


@router.post("/prompts/update", summary="保存新版本并生效（FR12：支持 5 层/灰度/门禁字段）", dependencies=_guard(PERM_PROMPT_WRITE))
async def prompt_update(
    body: PromptUpdate, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.prompt_manager.update(
        body.name, body.content, body.description,
        body.layer, body.rollout_status, body.gray_ratio, body.eval_passed,
    )


@router.post("/prompts/rollback", summary="回溯到历史版本（生成新版本，保留审计链）", dependencies=_guard(PERM_PROMPT_WRITE))
async def prompt_rollback(
    body: PromptRollback, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.prompt_manager.rollback(body.name, body.version)


@router.post("/prompts/eval", summary="记录评测结果（FR12 门禁前置）", dependencies=_guard(PERM_PROMPT_WRITE))
async def prompt_eval(
    body: PromptEval, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.prompt_manager.mark_eval(body.name, body.version, body.passed)


@router.post("/prompts/promote", summary="金丝雀晋级 full（FR12 评测门禁）", dependencies=_guard(PERM_PROMPT_WRITE))
async def prompt_promote(
    body: PromptPromote, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.prompt_manager.promote(body.name, body.version)


@router.post("/prompts/ab-compare", summary="A/B 双版本对比（FR12）", dependencies=_guard(PERM_PROMPT))
async def prompt_ab_compare(
    body: PromptAbCompare, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    return await runtime.prompt_manager.ab_compare(body.name, body.version_a, body.version_b)


@router.post("/prompts/eval-dataset", summary="数据集评测联动 FR12 发布门禁（P1-7）", dependencies=_guard(PERM_PROMPT_WRITE))
async def prompt_eval_dataset(
    body: PromptEvalByDataset, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    pm = runtime.prompt_manager
    if pm is None:
        return {"ok": False, "message": "提示词管理器未初始化"}
    return await pm.eval_by_dataset(
        body.name, body.version, body.answers,
        min_tcr=body.min_tcr,
        require_no_hallucination=body.require_no_hallucination,
    )
