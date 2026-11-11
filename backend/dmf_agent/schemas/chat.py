"""对话 API 模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户输入")
    session_id: Optional[str] = Field(None, description="会话 ID，为空则新建会话")
    user_id: str = Field("anonymous", description="用户标识")
    # WorkBuddy 式结构化澄清表单提交（反问澄清后用户点选确认的要素）：
    # {metrics:[...], dimensions:[...], time_range:..., extra:"", context:{...}}。
    # 传入时后端确定性合并要素并自动继续执行后续步骤（不重新抽取）。
    clarify_selection: Optional[dict] = Field(
        None, description="结构化澄清选择（指标/维度/时间），为空表示普通提问"
    )


class ChatResponse(BaseModel):
    session_id: str
    message_id: str
    content: str
    intent: Optional[str] = None
    # 查询后追加技能（迁移自老系统插件 B/C/E）：
    # chart_visualization / data_interpretation / complex_calculation
    skills: list[str] = []
    # 是否需要多次取数（对应老系统 plugin 序列中 A 重复出现）
    multi_query: bool = False
    # 记忆模块改写后的当前问题（与原输入一致表示未改写）
    rewritten_query: Optional[str] = None
    mock_mode: bool = False
    # P0-2 合规安全：输入命中安全规则被拦截（未进 Agent 主图）时为 True
    security_blocked: bool = False
    security_block_reason: Optional[str] = None
    # P0-1 Harness：输出经安全过滤/替换
    output_guarded: bool = False
    # P0-5 报错分析（FR6）：未捕获异常的结构化信息（错误码/分类/人工可读解释/是否可重试）
    error_info: Optional[dict] = None
    # 反问澄清（FR4）：需要用户确认要素时返回结构化表单载荷（前端渲染选择卡片）
    clarification: Optional[dict] = None
