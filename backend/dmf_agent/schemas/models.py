"""模型管理 API 模型（对话 LLM 配置）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ModelAdd(BaseModel):
    """新增一条对话 LLM 模型配置（OpenAI 兼容协议）。"""

    name: str = Field(..., min_length=1, max_length=64, description="模型标识（唯一）")
    model: str = Field(..., min_length=1, max_length=128, description="模型名，如 qwen-max")
    base_url: str = Field(..., min_length=1, max_length=512, description="OpenAI 兼容端点，如 https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key: str = Field("", description="API Key（自托管免鉴权可留空）")
    temperature: float = Field(0.1, ge=0, le=2, description="采样温度")
    timeout: int = Field(120, gt=0, description="请求超时（秒）")
    max_retries: int = Field(2, ge=0, description="失败重试次数")
    status: str = Field("enabled", description="enabled | disabled")
    is_default: bool = Field(False, description="是否设为默认（运行时生效模型）")


class ModelUpdate(BaseModel):
    """更新一条模型配置（按 name 精确定位；字段缺省则保持不变）。

    ``api_key`` 传空 / 缺省时保留原值（列表接口只回脱敏 key，更新界面通常留空）。
    """

    model: Optional[str] = Field(None, min_length=1, max_length=128)
    base_url: Optional[str] = Field(None, min_length=1, max_length=512)
    api_key: Optional[str] = Field(None, description="非空才更新；空 = 保留原 key")
    temperature: Optional[float] = Field(None, ge=0, le=2)
    timeout: Optional[int] = Field(None, gt=0)
    max_retries: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, description="enabled | disabled")
    is_default: Optional[bool] = Field(None)


class ModelProbe(BaseModel):
    """连接测试载荷（新增/编辑弹窗预检，不落库）。

    ``name`` 可选：编辑时传入已保存条目名，表单留空的字段（尤其 api_key 脱敏值）
    用库中已存配置补齐。
    """

    name: Optional[str] = Field(None, max_length=64)
    model: str = Field("", max_length=128)
    base_url: str = Field("", max_length=512)
    api_key: str = Field("")
    timeout: float = Field(30.0, gt=0, le=600, description="连接测试超时（秒）")
