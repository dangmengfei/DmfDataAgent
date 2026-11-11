"""查询改写节点（PRD §5.3.1「查询改写与黑话字典」）。

定位：**六要素识别之前的问题规范化环节**（FR16 / FR17）。

拓扑位置：`START -> rewrite_query -> classify_intent -> ...`

做三件事（顺序对齐 PRD 5.3.1 的"改写前置子流程"）：

1. **指代消解**：复用 `HistoryAssembler` 已产出的 `rewritten_query`
   （ChatService 注入 `state["context_rewritten"]`）——**不新增 LLM 调用**；
2. **黑话翻译 + 同义/缩写扩展 + 口语归一**：走 `SlangResolver`
   （三级词典：用户级 > 公司级 > 行业级；最长优先匹配；expand / replace / stop 三模式）；
3. **输出改写记录**：写 `state["query_rewrite"]`，随 trace 落库供审计与前端展示。

时间表达式（"上个月 / 近30天"）由 `time` 模块负责，本节点**不碰**。

降级保证：字典不可用 / 未命中 / 抛异常 -> **原句透传** + `degraded` 标记，绝不阻断对话。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.core.state import AgentState, last_human_text
from dmf_agent.recall.slang import hash_query

logger = logging.getLogger(__name__)


def _pick_input(state: AgentState) -> str:
    """改写输入：多轮确认拼接句 > 指代消解结果 > 原始问句。

    指代消解结果来自 L1 历史装配（相关性检查副产品），优先级高于原始问句，
    因为它已经把"它的环比 / 这季度"补全成了实体。
    """
    confirm = state.get("confirm_combined")
    if confirm:
        return str(confirm)
    contextual = state.get("context_rewritten")
    if isinstance(contextual, str) and contextual.strip():
        return contextual
    return last_human_text(state)


def build_rewrite_query_node(deps: AgentDeps):
    async def rewrite_query(state: AgentState) -> Dict[str, Any]:
        original = last_human_text(state)
        text = _pick_input(state)
        resolver = deps.slang_resolver

        # P0-2 输入安全预检：命中提示注入 / 越权意图 / 敏感词直接拦截
        security = getattr(deps, "security", None)
        if security is not None:
            check = security.check_input(original)
            if check.get("blocked"):
                logger.warning("输入安全拦截：%s", check.get("reason"))
                await security.audit_security(
                    event_type="security/input_guard",
                    level=check.get("risk_level", "high"),
                    summary=check.get("reason") or "输入安全拦截",
                    session_id=state.get("session_id") or "",
                    user_id=state.get("user_id") or "",
                    tenant_id=state.get("tenant_id") or "",
                    round_id=state.get("round_id") or "",
                    detail={"hits": check.get("hits", []), "input_length": len(original)},
                )
                return {
                    "query_original": original,
                    "query_rewritten": original,
                    "query_rewrite": {
                        "original": original,
                        "rewritten": original,
                        "hits": [],
                        "degraded": True,
                        "error": check.get("reason"),
                    },
                    "security_blocked": True,
                    "security_block_reason": check.get("reason") or "输入安全拦截",
                    "final_answer": (
                        "您的输入被安全策略拦截。请避免尝试绕过权限、获取他人数据或注入指令。"
                        "如需查看明细，请联系管理员。"
                    ),
                }

        if resolver is None:
            # 字典未装配（记忆/词典关闭或降级）：原句透传，链路照常
            return {
                "query_original": original,
                "query_rewritten": text,
                "query_rewrite": {"original": original, "rewritten": text,
                                  "hits": [], "degraded": True,
                                  "error": "resolver unavailable"},
            }

        result = await resolver.resolve(
            text,
            tenant_id=state.get("tenant_id") or "",
            user_id=state.get("user_id") or "",
        )
        # 命中计数（管理页观察词条热度）；失败不影响主链路
        await resolver.bump_hits(result.hits)
        # 改写埋点（命中率/误改写率看板）；失败不影响主链路
        await resolver.record_attempt(
            query=text,
            hit_count=len(result.hits),
            tenant_id=state.get("tenant_id") or "",
            user_id=state.get("user_id") or "",
        )

        record: Dict[str, Any] = {
            "original": original,
            "input": text,
            "rewritten": result.rewritten,
            "query_hash": hash_query(text),
            "hits": [h.model_dump() for h in result.hits],
            "mode_counts": result.mode_counts,
            "degraded": result.degraded,
            "error": result.error,
            "cache_source": result.cache_source,
            "elapsed_ms": result.elapsed_ms,
        }
        logger.info(
            "查询改写：%r -> %r（命中 %d 条：%s）",
            text,
            result.rewritten,
            len(result.hits),
            "、".join(f"{h.term}->{h.standard}" for h in result.hits) or "无",
        )
        return {
            "query_original": original,
            "query_rewritten": result.rewritten or text,
            "query_rewrite": record,
        }

    return rewrite_query
