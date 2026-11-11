"""评测体系接口（P1-7）：数据集版本化 / badcase 回流 / LLM-as-judge / FR12 门禁联动。

- GET  /eval/datasets           列出所有数据集版本与用例数
- GET  /eval/dataset/{version}  某版本用例明细
- POST /eval/backflow          把点踩样本回流到评测库（可选追加进数据集）
- GET  /eval/badcases          当前 bad case 库
- POST /eval/judge             对单条回答做 LLM-as-judge（无 LLM 走确定性兜底）
- POST /prompts/eval-dataset   用数据集评测结果联动 FR12 发布门禁
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_permission
from dmf_agent.auth.roles import PERM_PROMPT, PERM_PROMPT_WRITE
from dmf_agent.eval.badcase import backflow_from_feedback, badcase_store
from dmf_agent.eval.dataset import list_datasets, load_dataset
from dmf_agent.eval.judge import judge_answer
from dmf_agent.llm.providers import get_chat_model
from dmf_agent.schemas.eval import (
    BackflowRequest,
    BackflowResponse,
    JudgeRequest,
    JudgeResponse,
)
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/eval", tags=["eval"])


def _guard(permission: str) -> list:
    return [Depends(require_permission(permission))]


def _get_llm(runtime: AppRuntime):
    """取一个 ChatModel 用于 judge；不可用时返回 None（judge 走确定性兜底）。

    优先库内默认模型（模型管理页的生效覆盖），其次环境变量 DMF_LLM-*；
    均未配置返回 None。原实现 `getattr(s.llm, "configured", False)` 该属性
    并不存在（恒 False），导致 judge 永远走确定性兜底 —— 此处一并修正。
    """
    try:
        s = runtime.settings
        override = runtime.model_registry.effective_llm if runtime.model_registry else None
        return get_chat_model(s, llm_override=override)
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 judge LLM 失败，走确定性兜底：%s", exc)
    return None


@router.get("/datasets", dependencies=_guard(PERM_PROMPT))
async def list_eval_datasets(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    return {"ok": True, "datasets": list_datasets()}


@router.get("/dataset/{version}", dependencies=_guard(PERM_PROMPT))
async def get_eval_dataset(
    version: str, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    ds = load_dataset(version)
    if ds is None:
        return {"ok": False, "message": f"数据集不存在：{version}"}
    return {
        "ok": True,
        "version": ds.version,
        "note": ds.note,
        "cases": [c.to_dict() for c in ds.cases],
    }


@router.post("/backflow", response_model=BackflowResponse, dependencies=_guard(PERM_PROMPT_WRITE))
async def eval_backflow(
    body: BackflowRequest, runtime: AppRuntime = Depends(get_runtime)
) -> BackflowResponse:
    db = runtime.db
    repo = getattr(db, "bad_case_repo", None)
    if repo is None:
        return BackflowResponse(imported=0, store_total=badcase_store().count(), cases=[])
    res = backflow_from_feedback(repo, dataset_version=body.dataset_version)
    return BackflowResponse(
        imported=res.get("imported", 0),
        store_total=res.get("store_total", badcase_store().count()),
        cases=res.get("cases", []),
    )


@router.get("/badcases", dependencies=_guard(PERM_PROMPT))
async def list_badcases(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    items = [r.to_dict() for r in badcase_store().list_all()]
    return {"ok": True, "total": len(items), "cases": items}


@router.post("/judge", response_model=JudgeResponse, dependencies=_guard(PERM_PROMPT))
async def judge_one(
    body: JudgeRequest, runtime: AppRuntime = Depends(get_runtime)
) -> JudgeResponse:
    llm = _get_llm(runtime)
    result = await judge_answer(llm, body.query, body.reference, body.answer)
    return JudgeResponse(
        faithfulness=result.faithfulness,
        hallucination=result.hallucination,
        accuracy=result.accuracy,
        rationale=result.rationale,
        source=result.source,
    )
