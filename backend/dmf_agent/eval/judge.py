"""LLM-as-judge：对单条回答做可信度 / 幻觉 / 准确率评判（P1-7 评测体系核心）。

- ``JudgeResult``：结构化评判结果（faithfulness / hallucination / accuracy / rationale）。
- ``judge_answer``：优先用 LLM 结构化输出（``astructured_invoke`` 四级降级），
  任意失败或 ``llm=None`` 时回落 ``deterministic_judge``（基于词面重叠的确定性启发），
  保证**无 LLM / 内存模式下评测仍可跑、可复现**（满足 PRD 幻觉率目标 0 的基线度量）。
- ``deterministic_judge``：纯函数，输入相同输出相同，便于单测与离线回归。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Sequence

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from dmf_agent.llm.structured import StructuredOutputError, astructured_invoke

logger = logging.getLogger(__name__)

# 词面 token：中文按字切、英文/数字按词切（覆盖中英文混排的银行指标场景）
_TOKEN = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


class JudgeRubric(BaseModel):
    """judge 的结构化输出 schema（LLM 通道）。"""

    faithfulness: float = Field(..., ge=0.0, le=1.0, description="回答与参考/事实的一致性 0~1")
    hallucination: bool = Field(..., description="是否存在幻觉（编造不存在的数据/口径）")
    accuracy: float = Field(..., ge=0.0, le=1.0, description="业务准确率 0~1")
    rationale: str = Field(..., description="判定理由（中文）")


class JudgeResult(BaseModel):
    """单条评判结果。"""

    faithfulness: float = 1.0
    hallucination: bool = False
    accuracy: float = 1.0
    rationale: str = ""
    # 判定来源：llm（模型）| deterministic（确定性兜底）
    source: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


_SYSTEM = (
    "你是一名严谨的数据问答质量评审。给定用户问题、参考事实（可能为数据/口径/标准答案），"
    "以及待评审的助手回答，请客观评判：\n"
    "1. faithfulness：回答是否忠实于参考事实（无编造、无张冠李戴），0~1；\n"
    "2. hallucination：回答是否存在幻觉（出现参考中不存在的关键数字/指标/结论），true/false；\n"
    "3. accuracy：业务口径与计算是否准确，0~1；\n"
    "4. rationale：一句话中文理由。"
)


def deterministic_judge(
    query: str, reference: str, answer: str
) -> JudgeResult:
    """确定性兜底评判（无 LLM 也可用）：基于词面重叠的启发式。

    - 有参考时：faithfulness = 答案与参考的 token 交并比（Jaccard）；
      hallucination = 参考非空且重叠极低（<0.15 且参考 token>3）视为疑似幻觉。
    - 无参考时：faithfulness 以 query 关键词在答案中的命中率代理，hallucination 默认 False。
    """
    ref_tokens = set(_tokenize(reference))
    ans_tokens = set(_tokenize(answer))
    if ref_tokens:
        inter = ref_tokens & ans_tokens
        union = ref_tokens | ans_tokens
        faith = len(inter) / len(union) if union else 1.0
        halluc = (len(ref_tokens) > 3) and (faith < 0.15)
        rationale = (
            f"参考词面重叠率 {faith:.2f}；"
            + ("命中极低，疑似幻觉" if halluc else "未触发幻觉阈值")
        )
        return JudgeResult(
            faithfulness=round(faith, 4),
            hallucination=halluc,
            accuracy=round(faith, 4),
            rationale=rationale,
            source="deterministic",
        )
    # 无参考：以问题关键词命中代理可信度
    q_tokens = set(_tokenize(query))
    if q_tokens:
        hit = sum(1 for t in q_tokens if t in ans_tokens) / len(q_tokens)
        return JudgeResult(
            faithfulness=round(hit, 4),
            hallucination=False,
            accuracy=round(hit, 4),
            rationale=f"无参考，按问题关键词命中率 {hit:.2f} 代理",
            source="deterministic",
        )
    return JudgeResult(
        faithfulness=1.0,
        hallucination=False,
        accuracy=1.0,
        rationale="无参考且无问题词面，默认通过",
        source="deterministic",
    )


async def judge_answer(
    llm: Any,
    query: str,
    reference: str,
    answer: str,
    *,
    config: Optional[dict] = None,
) -> JudgeResult:
    """对单条回答做评判。llm 为 None 或 LLM 调用失败时回落确定性兜底。"""
    if llm is None:
        return deterministic_judge(query, reference, answer)
    messages: Sequence[BaseMessage] = [
        HumanMessage(content=_SYSTEM),
        HumanMessage(
            content=(
                f"【用户问题】\n{query}\n\n"
                f"【参考事实】\n{reference or '（无）'}\n\n"
                f"【待评审回答】\n{answer}\n\n"
                "请按 JSON Schema 输出评判。"
            )
        ),
    ]
    try:
        rubric = await astructured_invoke(llm, JudgeRubric, messages, config=config)
        return JudgeResult(
            faithfulness=rubric.faithfulness,
            hallucination=rubric.hallucination,
            accuracy=rubric.accuracy,
            rationale=rubric.rationale,
            source="llm",
        )
    except StructuredOutputError as exc:
        logger.warning("judge LLM 通道失败，回落确定性兜底：%s", exc)
        return deterministic_judge(query, reference, answer)
    except Exception as exc:  # noqa: BLE001 - 任何异常都不应阻断评测流程
        logger.warning("judge 异常，回落确定性兜底：%s", exc)
        return deterministic_judge(query, reference, answer)
