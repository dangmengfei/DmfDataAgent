"""评测体系 API 模型（P1-7）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class JudgeRequest(BaseModel):
    """单条 judge 请求。"""

    query: str = Field(..., min_length=1, description="用户问题")
    reference: str = Field("", description="参考事实（数据/口径/标准答案），为空走无参考代理")
    answer: str = Field(..., min_length=1, description="待评审的助手回答")


class JudgeResponse(BaseModel):
    faithfulness: float
    hallucination: bool
    accuracy: float
    rationale: str
    source: str


class BackflowRequest(BaseModel):
    """点踩回流请求：把 bad_case_repo 中的点踩样本导入评测库。"""

    dataset_version: Optional[str] = Field(
        None, description="若给定，则把样本作为回归用例追加进该数据集"
    )


class BackflowResponse(BaseModel):
    imported: int
    store_total: int
    cases: List[Dict[str, Any]] = Field(default_factory=list)


class PromptEvalByDataset(BaseModel):
    """用数据集评测结果联动 FR12 发布门禁。"""

    name: str = Field(..., min_length=1, description="模板名")
    version: str = Field(..., min_length=1, description="版本")
    answers: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "逐用例断言：{name, intent_ok, metrics_ok, skills_ok, time_ok, "
            "attribution_ok, judge?(JudgeResult dict), answer?}"
        ),
    )
    min_tcr: float = Field(0.8, ge=0.0, le=1.0, description="TCR 门禁阈值")
    require_no_hallucination: bool = Field(True, description="是否要求幻觉率为 0")
