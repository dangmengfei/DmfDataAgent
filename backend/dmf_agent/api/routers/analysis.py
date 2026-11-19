"""分析模型试算接口（2026-11-18 数据分析能力提升 P1，方案 §四 管理面）。

POST /analysis/run：对调用方提供的 {columns, rows} 结果集运行指定分析模型。
- 供管理面（SemanticExplorer 试算 / PluginsPanel skill 调试）与脚本验证使用；
- 只做确定性计算，不触达数据源；模型失败返回 ok=false 结构（不 500）；
- 模型清单 GET /analysis/models 供前端下拉与 LLM 规划展示。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from dmf_agent.analysis.base import model_names
from dmf_agent.analysis.tools import run_analysis, plan_models_text
from dmf_agent.auth.dependencies import get_current_user

router = APIRouter()


class AnalysisRunRequest(BaseModel):
    model: str = Field(..., description="分析模型名（MODEL_REGISTRY 内）")
    query_result: Dict[str, Any] = Field(
        ..., description="结果集 {columns, rows}（来自最近一次取数或手填样例）"
    )
    params: Optional[Dict[str, Any]] = Field(default_factory=dict, description="模型参数")


@router.get("/analysis/models")
async def list_analysis_models(user=Depends(get_current_user)):
    """可用分析模型清单（名 + 描述 + 参数 schema）。"""
    from dmf_agent.analysis.base import get_model

    models = []
    for name in model_names():
        m = get_model(name)
        if m is not None:
            models.append(
                {"name": m.name, "description": m.description, "input_schema": m.input_schema}
            )
    return {"models": models, "plan_text": plan_models_text()}


@router.post("/analysis/run")
async def run_analysis_once(req: AnalysisRunRequest, user=Depends(get_current_user)):
    """对给定结果集运行一次分析模型（确定性计算，失败返回 ok=false 不抛 500）。"""
    result = run_analysis(req.query_result, req.model, req.params)
    return result.to_dict()
