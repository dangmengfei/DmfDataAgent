"""追问节点：要素缺失/不合规时向用户补充确认（多轮确认）。

迁移自老系统交互确认机制（plugin/get_indicators/interaction/）：
- 输出确认问题 + 挂起确认信息（problems + original_query，含本轮已组合文本），
  由 ChatService 落库到助手消息 node_trace；
- 用户下一条消息作为答复时，ChatService 组合"原始问题 + 答复"注入 confirm_combined，
  extract_elements 用组合文本重新抽取（等价老系统 isUserConfirmation /
  data_saved_when_confirm 的状态还原）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from dmf_agent.agents.metric_agent.clarification import build_clarification
from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.guidance import (
    build_llm_guidance_instruction,
    resolve_create_guidance,
)
from dmf_agent.core.state import AgentState, last_human_text, user_query

logger = logging.getLogger(__name__)

_CLARIFY_SYSTEM = (
    "你是指标分析助手的追问模块。系统在解析用户问题时发现以下信息缺失或无法识别，"
    "请用一句自然的中文向用户说明并引导补充（可适当给出候选项）：\n{problems}"
)

# 判定 LLM 是否已给出"创建入口"（命中则不再重复追加引导块）。
# 只认创建动作词：LLM 仅说"该指标未收录"却没告诉用户怎么建，仍要补确定性引导。
_CREATE_HINT_MARKERS = ("创建一个指标", "创建指标", "新建指标", "新增指标", "起草")


def _missed_metrics(state: AgentState) -> list[str]:
    return list((state.get("validation") or {}).get("missed_metrics") or [])


def _append_guidance(text: str, state: AgentState) -> str:
    """确定性兜底：确保"未命中 -> 引导创建"文案一定出现在追问里。

    只引导、不自动创建（创建须由用户显式发起，见 `guidance.py` 说明）。
    """
    guidance = resolve_create_guidance(state.get("validation"))
    if not guidance:
        return text
    if any(marker in text for marker in _CREATE_HINT_MARKERS):
        return text
    return f"{text.rstrip()}\n\n{guidance}" if text.strip() else guidance


async def _read_prior_clarification_count(deps: AgentDeps, session_id: Optional[str]) -> int:
    """读取上一轮已累计的澄清次数（FR4 上限判定用）。

    仓储未装配（测试/降级）或读取失败时回落 0，等价于「不限制」（由节点正常追问）。
    """
    repo = getattr(deps, "conv_state_repo", None)
    if repo is None or not session_id:
        return 0
    try:
        prior = await repo.get(session_id)
        return prior.clarification_count if prior else 0
    except Exception as exc:  # noqa: BLE001 - 读取失败不应阻断追问
        logger.warning("读取 prior 澄清次数失败，回落为 0：%s", exc)
        return 0


def build_ask_clarification_node(deps: AgentDeps):
    async def ask_clarification(state: AgentState) -> Dict[str, Any]:
        problems = (state.get("validation") or {}).get("problems") or []
        problem_text = "；".join(problems)
        # 记录本轮已组合的查询文本（含多轮确认累计），供下一轮继续合并
        original_query = state.get("confirm_combined") or last_human_text(state)
        confirmation = {
            "problems": problems,
            "original_query": original_query,
            # WorkBuddy 式结构化确认：候选（按置信度排名）+ 预选值 + 提交回传上下文，
            # 由前端渲染为确认表单；用户提交后走 apply_clarification 确定性续跑。
            "clarification": build_clarification(deps, state, problems),
        }

        # FR8：未命中（指标名已提到但语义层未收录）-> 引导用户显式创建（不自动创建）
        missed = _missed_metrics(state)

        # FR4 反问澄清次数上限：读取 prior 澄清次数，达到上限后优雅终止，避免循环确认
        max_turns = deps.agent_settings.max_clarification_turns
        prior_count = await _read_prior_clarification_count(deps, state.get("session_id"))
        if prior_count >= max_turns:
            base = (
                "已多次向您确认仍未能明确具体查询内容，为避免循环确认，本轮不再追问。"
                "请直接给出明确的指标名称、口径与维度，例如："
                "“查一下近 7 天各分行存款余额排名”。"
            )
            return {"final_answer": _append_guidance(base, state)}
            # 不返回 confirmation -> ChatService 将状态置为 active 并清零澄清计数

        llm = deps.get_chat_model()

        if llm is not None:
            try:
                system = _CLARIFY_SYSTEM.format(problems=problem_text)
                llm_guidance = build_llm_guidance_instruction(missed)
                if llm_guidance:
                    system = f"{system}\n\n{llm_guidance}"
                resp = await llm.ainvoke(
                    [SystemMessage(content=system), HumanMessage(content=user_query(state))]
                )
                content = resp.content if isinstance(resp.content, str) else str(resp.content)
                return {"final_answer": _append_guidance(content, state), "confirmation": confirmation}
            except Exception as exc:  # noqa: BLE001 - LLM 失败降级模板
                logger.warning("LLM 追问生成失败，回退模板：%s", exc)

        base = (
            f"为了完成本次指标查询，还需要补充以下信息：{problem_text}。"
            "请补充后重试，例如：\"查一下近7天的GMV\"。"
        )
        return {
            "final_answer": _append_guidance(base, state),
            "confirmation": confirmation,
        }

    return ask_clarification
