"""Agent 核心状态定义（LangGraph State）。

对应老系统会话内的 sa_message 结构（user_query / round_information /
post_message / round_data），在 LangGraph 中统一收敛为单一 State，
由各节点增量更新，替代老系统通过线程共享队列 + 事件回调的隐式传参方式。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

IntentType = str
# "metric_query" | "dimension_query" | "diagnosis" | "knowledge_qa" | "help" | "exit" | "chitchat"
# （dimension_query/diagnosis 由 2026-08-27 意图识别迁移引入；help/exit 为 2026-09 FR1 控制类意图）


class AgentState(TypedDict, total=False):
    # 会话消息（LangGraph 内置 reducer：追加合并）
    messages: Annotated[list[AnyMessage], add_messages]

    # 运行上下文
    session_id: str
    user_id: str
    # 当前用户所属租户（多租户隔离：知识检索按此过滤；空串/缺省表示不隔离）
    tenant_id: str
    # 当前用户角色（admin/analyst/viewer）与展开后的权限点集合（admin 为 ["*"]）。
    # 由 ChatService 从鉴权身份注入，供 Harness 工具级权限校验与数据权限审计使用。
    user_role: str
    user_permissions: list[str]
    # L2 用户偏好（记忆开启时由 ChatService 注入：[{key, value, source}]）；
    # 一键关闭记忆时为空列表（与 L1/L3 同步失效）
    preferences: list[dict[str, Any]]

    # ---- 以下为指标问答业务状态（按节点逐步填充） ----
    # classify_intent 节点产出
    intent: IntentType
    # 意图置信度 0~1（低置信可触发澄清）
    intent_confidence: float
    # 查询后追加技能（有序）：chart_visualization / data_interpretation / complex_calculation
    # （迁移自老系统插件序列 B/C/E）
    skills: list[str]
    # 是否需要多次取数（对应老系统 plugin 序列中 A 重复出现）
    multi_query: bool
    # extract_elements 节点产出：{metrics, dimensions, filters, time_range, order_by, period_comparison}
    query_elements: dict[str, Any]
    # validate_elements 节点产出：{ok, problems}
    validation: dict[str, Any]
    # build_dsl 节点产出：对齐 HM API 查询体（迁移自老 servers/dsl.py）
    dsl: dict[str, Any]
    # execute_query 节点产出：{columns, rows, mock}
    query_result: dict[str, Any]
    # analyze_data 节点产出：{summary, stats}
    analysis: dict[str, Any]
    # diagnose 节点产出（intent=diagnosis）：{ok, metric, change, dimensions,
    # same/opposite_contribution_list, smart_summary, global_summary}（Phase 4c）
    diagnosis: dict[str, Any]
    # create 节点产出（intent=create，FR8）：{ok, metric_key, name, status, problems, message}
    # 落库成功后 ok=True 含草稿 key；校验/落库失败 ok=False 含 problems/message
    create_result: dict[str, Any]
    # retrieve_knowledge 节点产出：[{content, source, score}]
    retrieved_docs: list[dict[str, Any]]
    # execute_skill 节点产出（FR14）：命中的 skill 名（用作路由信号）
    active_skill: str
    # execute_skill 节点产出（FR14）：SkillExecutor.run 的返回 {ok, skill, inputs, steps, answer, message}
    skill_result: dict[str, Any]

    # ---- 查询改写（PRD §5.3.1：六要素识别之前的问题规范化环节）----
    # 原始问句（审计用；库里永远存原句，改写只作用于 state）
    query_original: str
    # 改写后问句（黑话替换 + 口语归一后的规范问句）；下游全部节点读它
    query_rewritten: str
    # 改写记录：{original, rewritten, hits[], mode_counts{}, degraded, error, elapsed_ms, cache_source}
    query_rewrite: dict[str, Any]
    # 指代消解输入：ChatService 从 HistoryAssembler.rewritten_query 注入（复用既有能力，不新增 LLM 调用）
    context_rewritten: str
    # 结构化对话上下文（P0-6 多轮对话状态管理）：ChatService 从上一轮
    # `conversation_states` 读取并注入，形如
    # {intent, active_metrics, active_dimensions, active_filters, time_range, topic, turn_count}。
    # 要素抽取 / 改写节点据此对"它 / 这个指标 / 按地区拆分 / 和上个月比"做确定性指代消解。
    context_entities: dict[str, Any]

    # 多轮确认（Phase 6）：上轮挂起确认时，ChatService 组合"原始问题+答复"注入，
    # extract_elements 用它替代 last_human_text 重新抽取
    confirm_combined: str
    # ask_clarification 产出：挂起确认信息 {problems, original_query, clarification}，
    # 由 ChatService 落库到助手消息 node_trace（clarification 为结构化确认表单载荷，
    # 前端据此渲染指标/维度/时间选择），下一轮据此组合查询
    confirmation: dict[str, Any]
    # 结构化澄清提交（WorkBuddy 式确认表单）：用户在前端确认后的选择，由 ChatService 注入。
    # 存在时图从 apply_clarification 入口开始，确定性合并要素后直接续跑
    # 校验 -> 编译 -> 取数链路（不再重新抽取，避免再次误解）。
    clarify_selection: dict[str, Any]
    # apply_clarification 节点产出：标记本轮由澄清提交续跑（观测 / 留痕用）
    clarification_resolved: bool

    # ---- P1-2 Sub-Agent 编排（FR10）----
    # subagent_dispatch 节点命中多子任务并行编排后置 True，主图据此跳过常规取数、
    # 直接进 synthesize_answer 聚合子结果；否则为 False（走常规单链路）。
    subagent_dispatched: bool
    # 各子任务执行结果（结构化，落库 / 前端展示用；非业务状态，仅透传）
    subagent_results: list

    # 终态：最终答复（ask_clarification / synthesize_answer / 安全拦截 产出）
    final_answer: str
    # 是否运行在降级 mock 模式（LLM 未配置等）
    mock_mode: bool
    # ---- P0-1 Harness 强约束：单轮计数器（max_rounds / tool_calls / token_budget / timeout）----
    harness_state: dict[str, Any]
    # ---- P0-2 合规安全：输入被拦截时直接跳至终态 ----
    security_blocked: bool
    security_block_reason: str


def last_human_text(state: AgentState) -> str:
    """取最近一条用户输入文本。"""
    for msg in reversed(state.get("messages", [])):
        if msg.__class__.__name__ == "HumanMessage":
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            return str(content)
    return ""


def user_query(state: AgentState) -> str:
    """用户问句（**改写后优先**），下游节点统一用它替代 `last_human_text`。

    链路：`rewrite_query` 节点产出 `query_rewritten` -> 意图识别 / 要素抽取 /
    RAG 检索 / 分析 / 答复都读改写后的规范问句；节点未跑或未命中时自动回退原句。
    """
    rewritten = state.get("query_rewritten")
    if isinstance(rewritten, str) and rewritten.strip():
        return rewritten
    return last_human_text(state)
