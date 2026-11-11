"""Skill 执行节点（FR14：对话内触发已上传的声明式 skill）。

放在「validate_elements 通过」之后：此时 `query_elements` 已就绪（输入绑定的来源），
用户点到澄清表单续跑时也能经 `apply_clarification → validate_elements → execute_skill`
自然续上。沿用了 FR10 `subagent_dispatch` 的**透传惯用法**：未命中任何 skill 就 `return {}`
（不改状态，路由继续走原取数链路），拓扑行为与未接线时完全一致。

选择顺序（对齐 Claude Code / Codex 的机制）：

1. **模型驱动优先**（`DMF_SKILL-MODEL_DRIVEN=true`，默认）：把 skill 清单
   （名 + 描述）交给 LLM，由模型判断本轮是否该调用某个 skill（可返回 none）；
2. 模型不可用 / 返回 none / 路由失败 → `match_package` 的 **triggers 确定性命中**兜底
   （无 LLM 也能触发，守住降级矩阵）；
3. 仍未命中 → 透传回 `build_dsl` 原链路。

`DMF_SKILL-MODEL_DRIVEN=false` 时回到旧顺序：triggers 确定性命中优先，
未命中且 `DMF_SKILL-MATCH_LLM_FALLBACK=true` 时再由 LLM 兜底选。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, user_query
from dmf_agent.harness import HarnessContext, HarnessState
from dmf_agent.skills import SkillPackage, bind_inputs, match_package

logger = logging.getLogger(__name__)


class _SkillPick(BaseModel):
    """模型驱动路由的返回：命中的 skill 名，或字面量 none。"""

    name: str = Field(description="最相关 skill 的 name；都不相关则 exactly 返回 none")


def _pick_prompt(packages: List[SkillPackage]) -> str:
    lines = [f"- {p.name}：{p.description or '（无描述）'}" for p in packages]
    return (
        "你是技能选择器。根据用户问题，从下面的技能清单里选一个最相关的技能；"
        "若都不相关，必须返回 none，不要硬凑。\n"
        + "\n".join(lines)
    )


def build_execute_skill_node(deps: AgentDeps):
    async def _load_packages() -> List[SkillPackage]:
        registry = getattr(deps, "plugin_registry", None)
        if registry is None:
            return []
        # 一次拉全部声明式包（内置与手动登记项天然排除），避免逐个 name 再拉详情的 N+1 查询
        return await registry.list_skill_packages()

    async def execute_skill(state: AgentState) -> Dict[str, Any]:
        cfg = deps.skill_settings
        if not (cfg.enabled and cfg.wire_into_graph):
            return {}
        if getattr(deps, "skill_executor", None) is None:
            return {}

        pkg: Optional[SkillPackage] = None
        try:
            packages = await _load_packages()
            if not packages:
                return {}  # 全新部署：零 LLM 调用，行为与未接线完全一致

            text = user_query(state)
            llm = deps.get_chat_model()

            if cfg.model_driven and llm is not None:
                pkg = await _llm_pick(llm, text, packages)  # 模型驱动优先
                if pkg is None:
                    # 模型说"没有相关技能"/路由失败 → triggers 确定性兜底（降级矩阵）
                    pkg = match_package(text, packages)
            elif cfg.model_driven:
                pkg = match_package(text, packages)  # 模型驱动但无 LLM：仅 triggers
            else:
                pkg = match_package(text, packages)  # 旧模式：triggers 优先
                if pkg is None and cfg.match_llm_fallback and llm is not None:
                    pkg = await _llm_pick(llm, text, packages)  # 未命中再由模型兜底

            if pkg is None:
                return {}  # 未命中：透传回 build_dsl 原链路

            inputs = bind_inputs(pkg, state.get("query_elements") or {})

            hctx = HarnessContext(
                session_id=state.get("session_id") or "",
                user_id=state.get("user_id") or "",
                tenant_id=state.get("tenant_id") or "",
                round_id=state.get("round_id") or "",
                user_permissions=list(state.get("user_permissions") or []),
            )
            harness_raw = state.get("harness_state") or {}
            harness_state = HarnessState(
                rounds=harness_raw.get("rounds", 0),
                tool_calls=harness_raw.get("tool_calls", 0),
                tokens_used=harness_raw.get("tokens_used", 0),
            )

            result = await deps.skill_executor.run(pkg, inputs, ctx=hctx, state=harness_state)
            return {
                "active_skill": pkg.name,
                "skill_result": result,
                "harness_state": harness_state.snapshot(),
            }
        except Exception as exc:  # noqa: BLE001 - 节点绝不抛异常，失败如实转报
            logger.warning("skill 触发执行失败：%s", exc)
            return {
                "active_skill": (pkg.name if pkg is not None else ""),
                "skill_result": {"ok": False, "skill": getattr(pkg, "name", ""), "message": f"执行失败：{exc}"},
            }

    return execute_skill


async def _llm_pick(llm: Any, text: str, packages: List[SkillPackage]) -> Optional[SkillPackage]:
    """LLM 从清单里选 skill；任何失败 / 非法 / 返回 none 即 None（放弃，走后续兜底）。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from dmf_agent.llm.structured import astructured_invoke

    by_name = {p.name: p for p in packages}
    try:
        pick = await astructured_invoke(
            llm,
            _SkillPick,
            [
                SystemMessage(content=_pick_prompt(packages)),
                HumanMessage(content=text),
            ],
        )
        name = (pick.name or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill 模型路由失败：%s", exc)
        return None
    if name.lower() == "none" or name not in by_name:
        return None
    return by_name[name]