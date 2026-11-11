"""指标问答主图（LangGraph StateGraph）。

拓扑：

    START
      └─> rewrite_query ──> classify_intent ──(metric_query / dimension_query / diagnosis)
      (查询改写：                  │          └─> extract_elements -> validate_elements
       黑话翻译 +                 │                                    │         (clarify / query)
       指代消解 +                 │(knowledge_qa)                      │      ask_clarification -> END
       口语归一)                  ▼                                    │      build_dsl -> execute_query
                              retrieve_knowledge                       │                │
                                      │                                │           analyze_data
                                      ▼                                ▼                │
                              synthesize_answer <───────────────────────── synthesize_answer
                                      │
                                     END

意图路由说明（2026-08-27 意图识别迁移）：
- metric_query / dimension_query / diagnosis 统一走取数链路
  （老系统"单一归因/绘图/解读插件自动前置取数"的等价实现；归因完整下钻链路 Phase 4 迁移）
- knowledge_qa 走 RAG 检索链路；chitchat 直接生成答复
- 查询后追加技能（skills：绘图/解读/复杂计算）与 multi_query 标记存于 state，随 trace 落库

空数据时间窗重定向（FR5 闭环，2026-10-28）：
- analyze_data 对空结果做 6 项回溯；首因为「查询结束日晚于数据截至日」且能确定性算出
  最新可得窗口时（empty_diagnosis.redirect），路由转 ask_time_redirect：下发
  「是否改查最新数据」结构化确认卡片后 END（不静默重试）。
- 用户确认 -> clarify_selection 从 apply_clarification 续跑（time_redirect 分支直接采用
  adjusted 窗口，指标/维度沿用）；已重定向过一次仍为空则不再弹卡（time_redirected 防循环）。

Skill 触发说明（2026-09-30 新增，FR14）：
- 校验通过后先过 `execute_skill`：按已上传 SKILL.md 的 triggers 确定性匹配
  （未命中且 LLM 可用时兜底路由），命中即在沙箱内执行并直接进答复；
- 未注册任何 skill / 未命中时节点**完全透传**回 build_dsl，拓扑行为与未接线一致（回归安全）；
- 输入按名绑定 query_elements（metric->metrics[0]、time->time_range），缺必填项由
  澄清追问补齐（见 skills/binding.py）。

查询改写说明（2026-09-14 新增，PRD §5.3.1）：
- `rewrite_query` 是**确定性节点**（查三级黑话词典 + 字符串改写），**不新增 LLM 调用**，
  耗时在个位数毫秒，不拖慢首 token；任一步异常都只原句透传，不阻断对话；
- 产出 `query_rewritten` 后，下游节点统一通过 `core.state.user_query(state)` 读取，
  使意图识别、要素抽取、RAG 检索、分析、答复都消费"规范问句"。

每个节点在 agents/metric_agent/nodes/ 下独立成文件，标注老系统迁移来源，
按模块逐步替换内部实现（见 MIGRATION.md）。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from dmf_agent.agents.metric_agent.deps import AgentDeps
from dmf_agent.agents.metric_agent.nodes import (
    build_analyze_data_node,
    build_apply_clarification_node,
    build_ask_clarification_node,
    build_ask_time_redirect_node,
    build_build_dsl_node,
    build_classify_intent_node,
    build_create_metric_node,
    build_diagnose_node,
    build_execute_query_node,
    build_execute_skill_node,
    build_extract_elements_node,
    build_retrieve_knowledge_node,
    build_rewrite_query_node,
    build_synthesize_answer_node,
    build_validate_elements_node,
)
from dmf_agent.agents.metric_agent.subagent import build_subagent_dispatch_node
from dmf_agent.core.state import AgentState


# 需要先走取数链路的意图（归因/维度查询在老系统中均先执行取数插件）
_DATA_CHAIN_INTENTS = frozenset({"metric_query", "dimension_query", "diagnosis"})
# 直接答复、不取数的意图（闲聊 / 帮助 / 退出）
_DIRECT_REPLY_INTENTS = frozenset({"chitchat", "help", "exit"})
# 新建指标意图（FR8）：起草草稿后直接进答复，不取数
_CREATE_INTENTS = frozenset({"create"})


def _route_by_intent(state: AgentState) -> str:
    intent = state.get("intent", "chitchat")
    if intent in _DATA_CHAIN_INTENTS:
        return "data_chain"
    if intent == "knowledge_qa":
        return "knowledge_qa"
    if intent in _CREATE_INTENTS:
        return "create"
    if intent in _DIRECT_REPLY_INTENTS:
        return "chitchat"
    return "chitchat"


def _route_after_subagent(state: AgentState) -> str:
    """子 Agent 派发后路由：已派发（多子任务命中）-> 直接答复；否则退回常规取数链路。"""
    return "finalize" if state.get("subagent_dispatched") else "query"


def _route_after_rewrite(state: AgentState) -> str:
    return "blocked" if state.get("security_blocked") else "continue"


def _route_entry(state: AgentState) -> str:
    """入口路由：结构化澄清提交 -> 直接合并要素续跑；否则走常规改名/识别链路。

    用户在确认表单上点选提交时（`clarify_selection` 由 ChatService 注入），
    要素已经明确，不必再过 rewrite / classify / extract（重复抽取反而可能再次误解）。
    """
    return "clarify" if state.get("clarify_selection") else "normal"


def _route_after_validation(state: AgentState) -> str:
    validation = state.get("validation") or {}
    return "clarify" if not validation.get("ok", False) else "query"


def _route_after_skill(state: AgentState) -> str:
    """skill 触发路由：命中（execute_skill 写入 active_skill）——直接答复；否则退回取数。"""
    return "skill" if state.get("active_skill") else "data"


def _route_after_analysis(state: AgentState) -> str:
    """分析后路由：
    - 空数据且首因为「查询时间晚于数据截至日」、可确定性给出最新窗口 -> ask_time_redirect
      （FR5 闭环：先问用户是否改查最新数据，不静默重试；空结果无归因下钻意义，优先于 diagnose）；
    - diagnosis 意图追加归因下钻；否则进答复。
    """
    empty_diag = (state.get("analysis") or {}).get("empty_diagnosis") or {}
    if empty_diag.get("redirect"):
        return "redirect"
    return "diagnose" if state.get("intent") == "diagnosis" else "finalize"


def build_metric_agent_graph(deps: AgentDeps) -> CompiledStateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("rewrite_query", build_rewrite_query_node(deps))
    graph.add_node("classify_intent", build_classify_intent_node(deps))
    graph.add_node("apply_clarification", build_apply_clarification_node(deps))
    graph.add_node("extract_elements", build_extract_elements_node(deps))
    graph.add_node("validate_elements", build_validate_elements_node(deps))
    graph.add_node("ask_clarification", build_ask_clarification_node(deps))
    graph.add_node("ask_time_redirect", build_ask_time_redirect_node(deps))
    graph.add_node("build_dsl", build_build_dsl_node(deps))
    graph.add_node("execute_query", build_execute_query_node(deps))
    graph.add_node("analyze_data", build_analyze_data_node(deps))
    graph.add_node("diagnose_data", build_diagnose_node(deps))
    graph.add_node("retrieve_knowledge", build_retrieve_knowledge_node(deps))
    graph.add_node("create_metric", build_create_metric_node(deps))
    graph.add_node("synthesize_answer", build_synthesize_answer_node(deps))

    # PRD FR10 Sub-Agent 编排：默认接入 `subagent_dispatch` 节点（wire_into_graph=True）。
    # 单任务 / 未识别到拆分时该节点完全透传，拓扑与历史单图行为一致（回归安全）。
    wire = deps.subagent_settings.wire_into_graph
    if wire:
        graph.add_node("subagent_dispatch", build_subagent_dispatch_node(deps))

    # PRD FR14 声明式 Skill：validate 通过后尝试触发（triggers 命中才执行，未命中完全透传）。
    wire_skill = deps.skill_settings.enabled and deps.skill_settings.wire_into_graph
    if wire_skill:
        graph.add_node("execute_skill", build_execute_skill_node(deps))

    # 入口分流：结构化澄清提交（用户已在表单确认要素）-> 合并后续跑；否则常规链路
    graph.add_conditional_edges(
        START,
        _route_entry,
        {"clarify": "apply_clarification", "normal": "rewrite_query"},
    )
    # 合并后的要素直接进校验（不通过则再次澄清，仍受单轮澄清次数上限约束）
    graph.add_edge("apply_clarification", "validate_elements")
    graph.add_conditional_edges(
        "rewrite_query",
        _route_after_rewrite,
        {"blocked": END, "continue": "classify_intent"},
    )
    graph.add_conditional_edges(
        "classify_intent",
        _route_by_intent,
        {
            "data_chain": "subagent_dispatch" if wire else "extract_elements",
            "knowledge_qa": "retrieve_knowledge",
            "create": "create_metric",
            "chitchat": "synthesize_answer",
        },
    )
    if wire:
        # 已派发多子任务 -> 直接答复；否则退回常规取数链路（extract -> ... -> analyze）
        graph.add_conditional_edges(
            "subagent_dispatch",
            _route_after_subagent,
            {"finalize": "synthesize_answer", "query": "extract_elements"},
        )
    graph.add_edge("extract_elements", "validate_elements")
    graph.add_conditional_edges(
        "validate_elements",
        _route_after_validation,
        # 校验通过时先过 skill 触发（未注册/未命中即透传），再进常规取数
        {"clarify": "ask_clarification", "query": "execute_skill" if wire_skill else "build_dsl"},
    )
    if wire_skill:
        # 命中 skill -> 直接答复；未命中 -> 退回常规取数链路（build_dsl -> ... -> analyze）
        graph.add_conditional_edges(
            "execute_skill",
            _route_after_skill,
            {"skill": "synthesize_answer", "data": "build_dsl"},
        )
    graph.add_edge("ask_clarification", END)
    # FR5 空数据时间窗重定向确认：用户确认后带 clarify_selection 从 apply_clarification 续跑
    graph.add_edge("ask_time_redirect", END)
    graph.add_edge("build_dsl", "execute_query")
    graph.add_edge("execute_query", "analyze_data")
    graph.add_conditional_edges(
        "analyze_data",
        _route_after_analysis,
        {
            "redirect": "ask_time_redirect",
            "diagnose": "diagnose_data",
            "finalize": "synthesize_answer",
        },
    )
    graph.add_edge("diagnose_data", "synthesize_answer")
    graph.add_edge("create_metric", "synthesize_answer")
    graph.add_edge("retrieve_knowledge", "synthesize_answer")
    graph.add_edge("synthesize_answer", END)

    return graph.compile()
