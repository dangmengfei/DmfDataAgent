"""点赞 / 点踩（few-shot & bad case）相关 DTO。

- FewShotData / BadCaseData：两条新表的仓储层数据对象
- FeedbackRequest：前端提交反馈的请求体
- FeedbackStatusResponse：批量查询某批消息的反馈状态（历史消息回显用）
- FewShotUpsert / FewShotPatch：Few-Shot 管理面的手工录入 / 编辑请求体
- FewShotRecallRequest / FewShotRecallResult：Few-Shot 管理面的「试召回」请求与结果
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class FewShotData(BaseModel):
    """few-shot 样本（经验库一条记录）。

    两个来源共用本表（`source` 区分），召回时一并参与 BM25 相似度匹配：
    - like   用户点赞助手回答自动沉淀（`session_id` / `message_id` 指向原消息，`score` 为点赞次数）
    - manual 管理面手工录入 / 批量导入（无原始消息，`session_id` / `message_id` 为空）

    `status=disabled` 的样本不参与召回（管理面「停用」而不删除，保留审计）。
    """

    id: int
    session_id: str = ""
    message_id: str = ""
    intent: Optional[str] = None
    category: str = "intention"
    question: str
    answer: str
    score: int = 1
    # like | manual（点赞沉淀 / 手工录入）
    source: str = "like"
    # active | disabled（disabled 不参与召回）
    status: str = "active"
    # 召回重排权重（与 domain_memories.weight 同义；点赞样本默认 1.0）
    weight: float = 1.0
    # 逗号分隔标签（管理面筛选 / 备注）
    tags: str = ""
    created_at: Optional[datetime] = None


class FewShotUpsert(BaseModel):
    """手工新增一条 few-shot 样本（写入 source='manual'）。"""

    question: str = Field(..., min_length=1, max_length=4000, description="示例问题")
    answer: str = Field("", max_length=8000, description="期望输出 / 参考答案")
    intent: Optional[str] = Field(None, max_length=32, description="期望意图（用于注入 prompt 的判定参考）")
    category: str = Field("intention", max_length=32, description="样本类别（意图识别用 intention）")
    tags: str = Field("", max_length=500, description="逗号分隔标签")
    weight: float = Field(1.0, ge=0.0, le=10.0, description="召回重排权重")


class FewShotPatch(BaseModel):
    """编辑一条 few-shot 样本（字段均可选，仅传需要改的）。"""

    question: Optional[str] = Field(None, min_length=1, max_length=4000)
    answer: Optional[str] = Field(None, max_length=8000)
    intent: Optional[str] = Field(None, max_length=32)
    category: Optional[str] = Field(None, max_length=32)
    tags: Optional[str] = Field(None, max_length=500)
    weight: Optional[float] = Field(None, ge=0.0, le=10.0)
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")


class FewShotRecallRequest(BaseModel):
    """试召回：用一句真实提问，验证哪些样本会被命中、最终拼出的注入文本长什么样。"""

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(3, ge=1, le=20)
    fail_on_empty: bool = Field(
        False, description="为 true 且无命中时返回 404（便于脚本化校验）"
    )


class FewShotRecallHit(BaseModel):
    """单条命中样本（含 BM25 分与重排分，便于解释"为什么它排在前面"）。"""

    id: int
    source: str
    question: str
    answer: str = ""
    intent: Optional[str] = None
    category: str = "intention"
    tags: str = ""
    weight: float = 1.0
    score: int = 1
    # BM25 原始相似度
    bm25: float = 0.0
    # 重排后的最终分（bm25 × 权重因子）
    rerank: float = 0.0


class FewShotRecallResult(BaseModel):
    """试召回结果：命中样本 + 实际注入到意图识别 prompt 的文本。"""

    query: str
    top_k: int
    candidates: int = Field(0, description="参与召回的样本总数（active 且 question/intent 非空）")
    hits: List[FewShotRecallHit] = Field(default_factory=list)
    injected_text: str = ""


class FewShotImportRequest(BaseModel):
    """批量导入样本；items 为导出同构结构或简写的 {question, answer}。

    items 声明为 `List[Any]` 而非 `List[Dict]`：导入是"尽力而为"的批量操作，
    单条格式不对时应当**跳过该条并回报行号**，而不是整个请求 422 让用户
    面对 pydantic 的 loc 报错（逐条校验在路由里做，错误进 `errors`）。
    """

    items: List[Any] = Field(default_factory=list)
    category: str = Field("intention", max_length=32)
    # 覆盖同 question 的已有样本（默认跳过重复，避免导入把人工修正覆盖掉）
    overwrite: bool = False


class FewShotImportResult(BaseModel):
    """导入结果统计。"""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)


class BadCaseData(BaseModel):
    """点踩样本（bad case 一条记录）。"""

    id: int
    session_id: str
    message_id: str
    intent: Optional[str] = None
    question: str
    answer: str
    reason: Optional[str] = None
    created_at: Optional[datetime] = None


class FeedbackRequest(BaseModel):
    """前端提交反馈：点赞 / 点踩某条助手消息。"""

    message_id: str = Field(..., description="助手消息 id（= trace_records.round_id）")
    # 选填：点赞时若前端已知 session_id 一并带上，缺省后端从 messages 表反查
    session_id: Optional[str] = None
    type: Literal["like", "dislike", "cancel"] = Field(
        ...,
        description="like=点赞进 few-shot；dislike=点踩进 bad case；cancel=取消当前反馈（type 传上一次的类型）",
    )
    # 点踩原因（可选）
    reason: Optional[str] = None


class FeedbackStatusResponse(BaseModel):
    """批量反馈状态查询响应：{message_id: 'like' | 'dislike'}。"""

    status: Dict[str, str] = Field(default_factory=dict)
