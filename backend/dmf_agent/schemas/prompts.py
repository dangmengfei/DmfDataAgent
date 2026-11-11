"""系统提示词管理 API 模型（FR12：5 层标注 + 灰度 + 评测门禁 + A/B）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# FR12：5 层标注取值
PROMPT_LAYERS = ["system", "task", "tool", "postprocess", "security"]
# FR12：灰度状态取值
ROLLOUT_STATES = ["full", "canary", "disabled"]


class PromptUpdate(BaseModel):
    name: str = Field(..., min_length=1, description="模板名")
    content: str = Field(..., min_length=1, description="新的 system 提示词内容")
    description: str = Field("", description="变更说明")
    # FR12：5 层标注
    layer: str = Field("", description="5 层之一：system/task/tool/postprocess/security")
    # FR12：灰度
    rollout_status: str = Field("full", description="rollout_status：full/canary/disabled")
    gray_ratio: float = Field(1.0, ge=0.0, le=1.0, description="金丝雀命中比例 0~1")
    # FR12：评测门禁
    eval_passed: bool = Field(False, description="评测是否通过（canary 晋级 full 前必须 true）")


class PromptRollback(BaseModel):
    name: str = Field(..., min_length=1, description="模板名")
    version: str = Field(..., min_length=1, description="要回溯到的版本")


class PromptPromote(BaseModel):
    name: str = Field(..., min_length=1, description="模板名")
    version: str = Field(..., min_length=1, description="要晋级的金丝雀版本")


class PromptEval(BaseModel):
    name: str = Field(..., min_length=1, description="模板名")
    version: str = Field(..., min_length=1, description="版本")
    passed: bool = Field(True, description="评测是否通过")


class PromptAbCompare(BaseModel):
    name: str = Field(..., min_length=1, description="模板名")
    version_a: str = Field(..., min_length=1, description="对照版本 A")
    version_b: str = Field(..., min_length=1, description="对照版本 B")
