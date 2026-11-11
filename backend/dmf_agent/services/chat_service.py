"""对话编排服务：会话管理 + 记忆装配 + LangGraph 执行 + 流式事件产出。

对应老系统 servers/sse_handler.py（老系统会话更新器 RoundUpdater 的
SSE 事件推送与 sa_message 组装逻辑）：
- 事件类型：session / node / token / final / error
- node_trace 落库字段对应老系统 sa_message.round_information（过程追踪）
- 历史装配委托 memory.HistoryAssembler（相关性检查 + 滚动压缩 + query 改写，
  迁移自老系统 RoundCompressor）
- 观测：遵循 OpenTelemetry —— 每轮 root span `chat_round` + 每个图节点一条子 span
  `node.<name>`（起止时间取 LangGraph 节点事件的真实 timestamp），轮末落 `trace_spans`；
  轮级汇总落 `trace_records`（前端"运行观测"页 / 对话内观测信息数据源）；
  另可选文件留痕（合规审计，落可配置目录，见 observability/），未配置时优雅跳过。
  不再导出到 agent-insight / 任何外部 OTLP collector。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage

from dmf_agent.config.settings import Settings
from dmf_agent.agents.metric_agent.errors import SessionBusyError, classify_exception
from dmf_agent.core.state import AgentState
from dmf_agent.db.base import (
    MessageRepository,
    SessionRepository,
    SpanRepository,
    TraceRepository,
    UserPreferenceRepository,
)
from dmf_agent.memory.history import HistoryAssembler
from dmf_agent.observability import (
    SessionTraceArchiver,
    build_round_tracer,
    iso_to_ns,
    otel_meta,
)
from dmf_agent.observability.cost import estimate_cost
from dmf_agent.observability.token_usage import TokenUsageCallback
from dmf_agent.schemas.session import MessageData
from dmf_agent.schemas.trace import TraceData
from dmf_agent.semantic.scope import bind_scope, reset_scope
from dmf_agent.services.session_lock import SessionBusy, SessionLock

logger = logging.getLogger(__name__)

# 过程追踪需要从节点更新中收集的字段（写入 messages.node_trace）
_TRACE_KEYS = (
    "intent", "skills", "multi_query", "query_elements",
    "validation", "dsl", "analysis", "retrieved_docs", "confirmation",
    "query_result",
    # FR10 Sub-Agent 编排：子任务派发标记与逐子任务结果随 trace 落库/前端
    "subagent_dispatched", "subagent_results",
    # FR14 声明式 Skill：命中的 skill 名与沙箱执行结果（steps 压缩，见下）
    "active_skill", "skill_result",
    # 查询改写记录（PRD §5.3.1：原句 / 改写句 / 命中词条 / 改写类型，随 Trace 留痕）
    "query_rewrite",
)
# trace 中 query_result 最多保留的行数（避免 node_trace 过大；前端据此绘图）
_MAX_TRACE_ROWS = 60
# trace 中查询改写命中的最多保留条数（避免长问句命中过多把 trace 撑大）
_MAX_TRACE_HITS = 20


def _filters_to_map(filters: Any) -> Dict[str, Any]:
    """把要素抽取的过滤条件列表（FilterCondition 或 dict）归一为 {字段: 值} 映射。

    供 P0-6 对话状态快照：下轮可按字段确定性沿用过滤；非列表 / 空值跳过。
    """
    out: Dict[str, Any] = {}
    if not isinstance(filters, list):
        return out
    for cond in filters:
        get = cond.get if isinstance(cond, dict) else lambda k, d=None: getattr(cond, k, d)
        field = get("field")
        if not field:
            continue
        values = get("values") or []
        operator = get("operator", "=")
        out[str(field)] = {"op": operator, "values": list(values)}
    return out


def _llm_model(settings: Optional[Settings]) -> str:
    """取当前 LLM 模型名（成本归集分组键；未配置返回空串，计价时回落兜底价）。"""
    llm = getattr(settings, "llm", None)
    return str(getattr(llm, "model", "") or "")


def _busy_event(session_id: str) -> Dict[str, Any]:
    """会话忙（等锁超时）的结构化错误事件：不落库、不污染历史，仅回报给前端。"""
    err = SessionBusyError()
    return {
        "type": "error",
        "session_id": session_id,
        "message_id": "",
        "content": err.user_message,
        "error_info": err.to_dict(),
    }


class ChatService:
    def __init__(
        self,
        graph: Any,
        session_repo: SessionRepository,
        message_repo: MessageRepository,
        settings: Settings,
        history_assembler: Optional[HistoryAssembler] = None,
        trace_repo: Optional[TraceRepository] = None,
        trace_archiver: Optional[SessionTraceArchiver] = None,
        span_repo: Optional[SpanRepository] = None,
        round_tracer_factory: Optional[Any] = None,
        # L2 用户偏好仓储（记忆开启时加载注入 prompt；None 表示不注入偏好）
        preference_repo: Optional[UserPreferenceRepository] = None,
        # 用户级"一键关闭记忆"开关（键为 user_id）；由 AppRuntime.memory_disabled 共享引用
        memory_disabled: Optional[dict] = None,
        # 合规安全层（PRD FR13）：输入守卫 + 数据访问审计；None 时跳过（测试/未装配）
        security: Optional[Any] = None,
        # 错误样本池（PRD FR6）：未捕获异常的报错分类样本回写；None 时跳过
        error_sample_repo: Optional[Any] = None,
        # 结构化对话状态（P0-6 多轮对话状态管理）：跨轮实体携带 + 确定性指代消解；
        # None 时跳过（测试 / 未装配）
        conversation_state_repo: Optional[Any] = None,
        # 请求级语义层解析（多租户）：按 tenant_id 取「全局链 + 租户 overlay」生效层，
        # 在图执行期绑到 ContextVar 上；None 时退化为进程级单一全局层（旧行为）
        semantic_scope: Optional[Any] = None,
        # 会话级串行化闸（services/session_lock.py）：同一会话的轮次互斥，避免共享
        # user_id 的多人并发写坏历史；None 时跳过（测试 / 未装配）
        session_lock: Optional[SessionLock] = None,
    ) -> None:
        self._graph = graph
        self._session_repo = session_repo
        self._message_repo = message_repo
        self._settings = settings
        self._history = history_assembler
        self._trace_repo = trace_repo
        self._trace_archiver = trace_archiver
        self._span_repo = span_repo
        self._preference_repo = preference_repo
        # 共享 AppRuntime.memory_disabled 的同一字典引用，路由开关即时生效
        self._memory_disabled: dict = memory_disabled if memory_disabled is not None else {}
        # 合规安全层（输入守卫 / 输出过滤 / 脱敏 / 权限审计）；未装配时所有安全检查优雅跳过
        self._security = security
        # 错误样本池（PRD FR6）：未捕获异常的报错分类样本回写；未装配时跳过
        self._error_sample_repo = error_sample_repo
        # 结构化对话状态（P0-6 多轮对话状态管理）；未装配时跳过
        self._conv_state_repo = conversation_state_repo
        # 请求级语义层解析（多租户）；未装配时所有租户共用进程级全局层
        self._semantic_scope = semantic_scope
        # 会话级串行化闸；未装配时不做串行化（回归默认为旧行为）
        self._session_lock = session_lock
        # 每轮新建一个 OTel tracer（独立 TracerProvider，避免多轮串扰）；
        # 观测关闭 / 依赖缺失时 build_round_tracer 返回 None，自动退化为"仅轮级落库"
        self._round_tracer_factory = round_tracer_factory or (
            lambda: build_round_tracer(self._settings)
        )

    async def ensure_session(
        self, session_id: Optional[str], user_id: str, message: str, tenant_id: str = ""
    ):
        if session_id:
            existing = await self._session_repo.get(session_id)
            if existing is not None:
                return existing
            logger.info("会话 %s 不存在，新建会话", session_id)
        title = message[:20] if message else "新会话"
        return await self._session_repo.create(user_id=user_id, title=title, tenant_id=tenant_id)

    # 记忆未装配（测试 / 未启用）时的空上下文状态
    _EMPTY_CONTEXT = {
        "memory_enabled": False, "total_rounds": 0, "uncovered_rounds": 0,
        "retain_rounds": 0, "threshold": 0, "used_tokens": 0, "max_tokens": 0,
        "ratio": 0.0, "has_summary": False, "compressible": False,
    }

    async def history_context_status(self, session_id: str) -> Dict[str, Any]:
        """上下文占比状态（轮次口径）；未装配历史装配器时返回空态。"""
        if self._history is None:
            return dict(self._EMPTY_CONTEXT)
        return await self._history.context_status(session_id)

    async def compress_history(self, session_id: str) -> Dict[str, Any]:
        """手动把更早轮次压缩为会话摘要；未装配时返回未启用结果。

        走同一把会话锁：与进行中的对话轮次互斥，撞车时返回 `reason=session_busy`
        而非硬等（压缩会整条覆盖摘要，绝不能与在跑的轮次并行）。
        """
        if self._history is None:
            return {"compressed": False, "reason": "history_unavailable", **dict(self._EMPTY_CONTEXT)}
        if self._session_lock is None:
            return await self._history.compress_now(session_id)
        try:
            async with self._session_lock.hold(session_id or ""):
                return await self._history.compress_now(session_id)
        except SessionBusy:
            return {
                "compressed": False,
                "reason": "session_busy",
                "message": "该会话正在回复中，请等本轮结束再压缩",
                **await self._history.context_status(session_id),
            }

    async def chat(
        self, *, session_id: Optional[str], user_id: str, message: str, tenant_id: str = "",
        user_role: str = "", user_permissions: Optional[list[str]] = None,
        clarify_selection: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """阻塞式对话，返回最终结果。"""
        final: Dict[str, Any] = {}
        error_event: Optional[Dict[str, Any]] = None
        async for event in self.astream_chat(
            session_id=session_id, user_id=user_id, message=message, tenant_id=tenant_id,
            user_role=user_role, user_permissions=user_permissions,
            clarify_selection=clarify_selection,
        ):
            if event["type"] == "final":
                final = event
            elif event["type"] == "error":
                error_event = event
        # 仅产出 error 事件（主图未达 final）时，构造结构化错误响应（避免 ChatResponse 缺字段 500）
        if error_event and not final:
            return {
                "session_id": error_event.get("session_id") or "",
                "message_id": error_event.get("message_id") or "",
                "content": error_event.get("content") or "服务内部错误",
                "error_info": error_event.get("error_info"),
            }
        return final

    async def astream_chat(
        self, *, session_id: Optional[str], user_id: str, message: str, tenant_id: str = "",
        user_role: str = "", user_permissions: Optional[list[str]] = None,
        clarify_selection: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式对话（会话级串行化闸：同一会话的轮次互斥，见 services/session_lock.py）。

        同一 user_id 多人 / 多标签页共用一个会话框时，并发轮次会交错落库、重复触发滚动压缩、
        互相覆盖结构化对话状态；这里把同一 session_id 的整轮串起来（新会话无并发直接放行）。
        等锁超时以「会话正在回复中」的结构化错误回报，不落库、不污染历史。
        """
        if self._session_lock is None:
            async for ev in self._astream_chat_impl(
                session_id=session_id, user_id=user_id, message=message, tenant_id=tenant_id,
                user_role=user_role, user_permissions=user_permissions,
                clarify_selection=clarify_selection,
            ):
                yield ev
            return
        try:
            async with self._session_lock.hold(session_id or ""):
                async for ev in self._astream_chat_impl(
                    session_id=session_id, user_id=user_id, message=message, tenant_id=tenant_id,
                    user_role=user_role, user_permissions=user_permissions,
                    clarify_selection=clarify_selection,
                ):
                    yield ev
        except SessionBusy:
            yield _busy_event(session_id or "")

    async def _astream_chat_impl(
        self, *, session_id: Optional[str], user_id: str, message: str, tenant_id: str = "",
        user_role: str = "", user_permissions: Optional[list[str]] = None,
        clarify_selection: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式对话实现体：产出 session / node / token / final / error 事件。

        `clarify_selection`：WorkBuddy 式结构化澄清表单的提交内容（指标/维度/时间等
        用户在界面上点选的结果）。传入时图入口直接走 `apply_clarification` 确定性合并，
        跳过改写/意图/抽取，随后自动继续校验 -> 编译 -> 取数 -> 分析 -> 答复。
        """
        started = time.time()

        # ---- P0-2 输入安全守卫（PRD FR13）：提示注入 / 越权意图 / 敏感词 / 超长 ----
        # 命中高危直接短路，不进 Agent 主图、不取数；审计留痕后返回安全提示。
        if self._security is not None:
            in_check = self._security.check_input(message)
            if in_check.get("blocked"):
                reason = in_check.get("reason") or "输入命中安全规则"
                try:
                    await self._security.audit_security(
                        event_type="security/input_guard",
                        level=in_check.get("risk_level") or "high",
                        summary=f"输入被拦截：{reason}",
                        user_id=user_id,
                        tenant_id=tenant_id,
                        detail={"hits": in_check.get("hits", [])[:20]},
                    )
                except Exception:  # noqa: BLE001 - 审计失败不影响拦截
                    logger.warning("输入拦截审计写入失败", exc_info=True)
                logger.info("输入安全拦截（user=%s risk=%s）：%s", user_id, in_check.get("risk_level"), reason)
                safe_reply = (
                    "抱歉，你的输入命中了安全规则（提示注入 / 越权操作 / 敏感内容 / 超长），"
                    "本次请求已被拦截。请调整提问方式，仅查询你有权访问的指标与数据。"
                )
                # 仍建会话并落用户消息与拦截答复，保证前端会话连续、审计可追溯
                session = await self.ensure_session(session_id, user_id, message, tenant_id=tenant_id)
                yield {"type": "session", "session_id": session.id, "title": session.title}
                user_saved = await self._message_repo.add(
                    MessageData(id="", session_id=session.id, role="user", content=message)
                )
                saved = await self._message_repo.add(
                    MessageData(
                        id="", session_id=session.id, role="assistant", content=safe_reply,
                        node_trace={
                            "security_blocked": True,
                            "security_block_reason": reason,
                            "question": message,
                        },
                    )
                )
                yield {
                    "type": "final",
                    "session_id": session.id,
                    "message_id": saved.id,
                    "user_message_id": user_saved.id,
                    "content": safe_reply,
                    "security_blocked": True,
                    "security_block_reason": reason,
                    "node_trace": {"security_blocked": True, "security_block_reason": reason},
                }
                return

        session = await self.ensure_session(session_id, user_id, message, tenant_id=tenant_id)
        yield {"type": "session", "session_id": session.id, "title": session.title}

        # 观测：遵循 OpenTelemetry —— 本轮 root span（chat_round）；
        # 节点子 span 由 LangGraph debug 事件的真实 timestamp 驱动（见下方循环）
        round_tracer = self._round_tracer_factory()
        if round_tracer is not None:
            round_tracer.start_round(
                session_id=session.id, user_id=user_id, message=message
            )
            round_tracer.set_session_id(session.id)

        # 记忆装配：相关性检查 + 滚动压缩 + query 改写。
        # 用户级"一键关闭记忆"（runtime.memory_disabled[user_id]=True）时退化为全量历史，
        # 等价 memory.enabled=False（PRD FR9 用户可一键关闭记忆注入）。
        memory_on = self._history is not None and not self._memory_disabled.get(user_id)
        if memory_on:
            assembled = await self._history.build(session.id, message, callbacks=None)
            lc_messages = assembled.messages
            rewritten_query = assembled.rewritten_query
        else:
            # 未注入装配器：退化为全量历史（等价 memory.enabled=False）
            history = await self._message_repo.list_by_session(session.id)
            lc_messages = []
            for m in history:
                if m.role == "user":
                    lc_messages.append(HumanMessage(content=m.content))
                elif m.role == "assistant":
                    lc_messages.append(AIMessage(content=m.content))
            lc_messages.append(HumanMessage(content=message))
            assembled = None
            rewritten_query = message

        # 留住本条用户消息的 id：终态事件会回传 `user_message_id`，供前端「编辑重发 →
        # 替换该轮」精准锚定要裁剪的那条提问（追加式编辑则不需要它）。
        user_saved = await self._message_repo.add(
            MessageData(id="", session_id=session.id, role="user", content=message)
        )

        # WorkBuddy 式结构化澄清提交：用户在确认表单上点选要素后提交。
        # 纯选择提交 -> 注入 clarify_selection，图入口走 apply_clarification 确定性合并，
        # 跳过改写/意图/抽取（避免再次误解），随后自动续跑取数链路。
        # 若附带自由补充说明（extra），则放弃确定性路径，改由常规 LLM 抽取理解补充语义
        # （前端会把"已确认选择 + 补充说明"合成为问句发送）。
        structured_clarify = None
        if isinstance(clarify_selection, dict) and clarify_selection:
            if str(clarify_selection.get("extra") or "").strip():
                logger.info("澄清提交含补充说明，改走常规抽取链路（由 LLM 理解补充语义）")
            else:
                structured_clarify = clarify_selection
                logger.info(
                    "澄清提交：注入结构化选择（metrics=%s dims=%s time=%s），图入口续跑",
                    clarify_selection.get("metrics"),
                    clarify_selection.get("dimensions"),
                    clarify_selection.get("time_range"),
                )

        # 多轮确认：上一条助手消息若带挂起确认，则本轮视为答复，
        # 组合"原始问题 + 答复"注入 confirm_combined 供要素抽取（等价老 isUserConfirmation）
        # （结构化提交路径不需要文本拼接，跳过）
        confirm_combined = None
        if structured_clarify is None:
            for history_msg in reversed(await self._message_repo.list_by_session(session.id)):
                if history_msg.role == "assistant":
                    confirmation = (history_msg.node_trace or {}).get("confirmation")
                    if confirmation and confirmation.get("original_query"):
                        confirm_combined = f"{confirmation['original_query']} {message}"
                    break

        state: AgentState = {
            "messages": lc_messages,
            "session_id": session.id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "user_role": user_role or "",
            "user_permissions": list(user_permissions or []),
            "round_id": session.id,  # 简化：用 session_id 作为 round_id（如需 message.id 可后续调整）
            "harness_state": {},
            "security_blocked": False,
            "security_block_reason": "",
        }
        # L2 用户偏好：与 L1/L3 同一开关（一键关闭记忆时同步不注入偏好）。
        # 只取 active 记录，按 confidence 降序（显式设置优先于系统推断）。
        if memory_on and self._preference_repo is not None:
            try:
                prefs = await self._preference_repo.list_by_user(user_id, status="active")
                if prefs:
                    state["preferences"] = [
                        {"key": p.key, "value": p.value, "source": p.source}
                        for p in sorted(prefs, key=lambda x: -float(x.confidence or 0))
                    ]
                    trace_prefs = [
                        f"{p['key']}={p['value']}" for p in state["preferences"]
                    ]
                    logger.info("已注入 L2 用户偏好 %d 条：%s", len(prefs), trace_prefs)
            except Exception as exc:  # noqa: BLE001 - 偏好读取失败不影响主链路
                logger.warning("读取 L2 用户偏好失败（跳过注入）：%s", exc)
        if confirm_combined:
            state["confirm_combined"] = confirm_combined
        if structured_clarify is not None:
            state["clarify_selection"] = structured_clarify
        # 指代消解输入：把 L1 历史装配产出的改写问句交给 rewrite_query 节点
        # （复用既有能力，不新增 LLM 调用；无历史 / 未开启时留空，节点自动回退原句）
        if rewritten_query:
            state["context_rewritten"] = rewritten_query
        # P0-6 结构化对话上下文：读取上一轮已解析实体，注入 context_entities，
        # 供要素抽取 / 改写节点对"它 / 这个指标 / 按地区拆分 / 和上个月比"做确定性指代消解。
        # 失败不影响主链路（降级为无上下文，退化为仅 L1 历史装配的模糊改写）。
        if self._conv_state_repo is not None:
            try:
                from dmf_agent.services.conversation_state import ConversationStateService

                ctx = await ConversationStateService(self._conv_state_repo).load_context(
                    session.id
                )
                if ctx:
                    state["context_entities"] = ctx
                    logger.info(
                        "注入对话上下文（turn=%s, metrics=%s, dims=%s）",
                        ctx.get("turn_count"), ctx.get("active_metrics"), ctx.get("active_dimensions"),
                    )
            except Exception as exc:  # noqa: BLE001 - 上下文读取失败不影响主链路
                logger.warning("读取对话上下文失败（跳过注入）：%s", exc)
        config: Dict[str, Any] = {
            "configurable": {"thread_id": session.id},
            "metadata": {"session_id": session.id, "user_id": user_id},
        }

        # token 用量统计：一次图执行内累计所有 LLM 调用（结果写入 trace 落库）
        token_cb = TokenUsageCallback()
        config["callbacks"] = [token_cb]

        final_answer = ""
        intent: Optional[str] = None
        skills: list = []
        multi_query = False
        mock_mode = False
        trace: Dict[str, Any] = {"question": message}
        # P0-6 对话状态快照采集（跨节点产出累积：query_original / query_rewritten /
        # query_elements / confirmation / intent 已在上方 update 循环收集）
        collected: Dict[str, Any] = {}
        # LangGraph debug 事件：task（节点开始）/ task_result（节点结束）→ 节点子 span
        open_node_spans: Dict[str, Any] = {}
        # 节点开始时间（流式期间向前端即时回报每步耗时，无需等 final）
        node_started_at: Dict[str, float] = {}

        # 多租户请求级装配：把该租户的语义层（全局链 + 租户 overlay）绑到本请求的
        # ContextVar 上，节点的 `deps.semantic_layer`（property）随之切到该租户的口径。
        # 解析失败不阻断：scope 为 None 时退回进程级全局层。
        request_scope = None
        if self._semantic_scope is not None:
            try:
                request_scope = await self._semantic_scope.scope_for(tenant_id)
                if request_scope is not None:
                    trace["semantic_scope"] = request_scope.describe()
            except Exception:  # noqa: BLE001 - 作用域解析失败按全局层继续
                logger.warning("语义层请求作用域解析失败（按全局层继续）", exc_info=True)
        scope_token = bind_scope(request_scope)

        try:
            async for mode, payload in self._graph.astream(
                state, config, stream_mode=["updates", "messages", "debug"]
            ):
                if mode == "messages":
                    chunk, meta = payload
                    if meta.get("langgraph_node") == "synthesize_answer":
                        content = chunk.content
                        if isinstance(content, str) and content:
                            final_answer += content
                            yield {"type": "token", "content": content}
                elif mode == "debug":
                    self._handle_debug_event(
                        payload, round_tracer, open_node_spans
                    )
                    # 节点开始/完成事件：实时回写耗时到前端（无需等 final）
                    evt_type = payload.get("type") if isinstance(payload, dict) else None
                    body = payload.get("payload") if isinstance(payload, dict) else None
                    if isinstance(body, dict):
                        node_name = body.get("name")
                        key = str(body.get("id") or node_name or "")
                        if node_name and key:
                            if evt_type == "task":
                                node_started_at[key] = time.time()
                            elif evt_type == "task_result":
                                started = node_started_at.pop(key, None)
                                has_err = bool(body.get("error"))
                                elapsed_ms = (
                                    round((time.time() - started) * 1000, 1)
                                    if started is not None else None
                                )
                                node_done_evt = {
                                    "type": "node_done",
                                    "node": node_name,
                                    "status": "error" if has_err else "ok",
                                }
                                if elapsed_ms is not None:
                                    node_done_evt["elapsed_ms"] = elapsed_ms
                                yield node_done_evt
                else:
                    for node, update in (payload or {}).items():
                        if not isinstance(update, dict):
                            continue
                        # 节点开始事件（updates 模式先来），带 started_at 给前端做"运行中"+实时秒表
                        yield {
                            "type": "node_start",
                            "node": node,
                            "started_at": time.time(),
                        }
                        if update.get("final_answer"):
                            final_answer = update["final_answer"]
                        if update.get("intent"):
                            intent = update["intent"]
                        if update.get("skills"):
                            skills = update["skills"]
                        if update.get("multi_query"):
                            multi_query = True
                        if update.get("mock_mode"):
                            mock_mode = True
                        # P0-6 对话状态快照采集：从各节点产出抽取已解析实体
                        if update.get("query_original"):
                            collected["query_original"] = update["query_original"]
                        if update.get("query_rewritten"):
                            collected["query_rewritten"] = update["query_rewritten"]
                        if update.get("query_elements"):
                            collected["query_elements"] = update["query_elements"]
                        if update.get("confirmation"):
                            collected["confirmation"] = update["confirmation"]
                        for key in _TRACE_KEYS:
                            if key in update:
                                value = update[key]
                                if key == "retrieved_docs":
                                    value = f"命中 {len(value or [])} 条"
                                elif key == "skill_result" and isinstance(value, dict):
                                    # 步骤只留摘要、答复截断，避免 node_trace 膨胀
                                    answer = value.get("answer")
                                    value = {
                                        **value,
                                        "answer": answer[:2000] if isinstance(answer, str) else answer,
                                        "steps": [
                                            {
                                                k: s.get(k)
                                                for k in ("index", "tool", "status", "elapsed_ms", "summary")
                                                if s.get(k) is not None
                                            }
                                            for s in value.get("steps") or []
                                        ],
                                    }
                                elif key == "query_result" and isinstance(value, dict):
                                    # 限制行数，避免 node_trace 过大（前端据此绘图）
                                    value = {
                                        **value,
                                        "rows": (value.get("rows") or [])[:_MAX_TRACE_ROWS],
                                    }
                                elif key == "query_rewrite" and isinstance(value, dict):
                                    # 命中条数封顶，避免长问句命中过多撑大 trace
                                    hits = value.get("hits") or []
                                    value = {
                                        **value,
                                        "hit_count": len(hits),
                                        "hits": hits[:_MAX_TRACE_HITS],
                                    }
                                trace[key] = value
        except Exception as exc:  # noqa: BLE001 - 对话异常统一回报
            logger.exception("Agent 执行失败")
            # PRD FR6：把任意原始异常收敛为结构化错误（含错误码 / 分类 / 人工可读解释）
            error = classify_exception(exc)
            trace["error"] = error.to_dict()
            # 未及收尾的节点 span 统一标记异常结束，避免 span 丢失
            for handle in open_node_spans.values():
                handle.end(error=True, message=str(exc))
            open_node_spans.clear()
            if round_tracer is not None:
                round_tracer.end_round(error=True, message=str(exc))
            # 入观测样本池（best-effort，失败不影响回报）
            if self._error_sample_repo is not None:
                try:
                    await self._error_sample_repo.add(
                        {
                            "session_id": session.id,
                            "user_id": user_id,
                            "tenant_id": tenant_id,
                            "round_id": session.id,
                            "error_code": error.code,
                            "category": int(error.category),
                            "category_label": error.to_dict()["category_label"],
                            "node": error.node or "",
                            "message": error.user_message,
                            "detail": error.detail or "",
                            "retryable": error.retryable,
                            "recovered": False,
                            "fix_hint": error.fix_hint or "",
                            "attempts": 1,
                            "trace_snippet": (message or "")[:500],
                        }
                    )
                except Exception:  # noqa: BLE001 - 采样失败不影响主链路
                    logger.warning("错误样本落库失败（忽略）", exc_info=True)
            # 落库一条错误助手消息（保证会话连续、前端可展示结构化错误）
            saved = await self._message_repo.add(
                MessageData(
                    id="",
                    session_id=session.id,
                    role="assistant",
                    content=error.user_message,
                    node_trace={"error": error.to_dict()},
                )
            )
            yield {
                "type": "error",
                "session_id": session.id,
                "message_id": saved.id,
                "user_message_id": user_saved.id,
                "content": error.user_message,
                "error_info": error.to_dict(),
            }
            return
        finally:
            # 作用域必须复位：异步生成器/任务可能复用同一 context，残留的租户层会让
            # 后续请求取到别的租户的口径 —— 这是数据正确性问题，不是体验问题。
            reset_scope(scope_token)

        # 记忆装配信息落入 trace（对应老系统 rewritten_query / is_related_to_history）
        if assembled is not None:
            trace["rewritten_query"] = assembled.rewritten_query
            trace["related_to_history"] = assembled.related_to_history
            trace["history_compressed"] = assembled.compressed
        # L2 用户偏好同样入 trace：前端可展示"本轮参考了哪些偏好"，便于排查口径偏差
        if state.get("preferences"):
            trace["preferences"] = state["preferences"]

        # 观测：OTel root span 收尾（属性写入轮级指标，便于按 trace 回溯）
        elapsed_ms = round((time.time() - started) * 1000, 1)
        if round_tracer is not None:
            round_tracer.end_round(
                error=bool(trace.get("error")),
                attributes={
                    "intent": intent or "",
                    "mock_mode": mock_mode,
                    "multi_query": multi_query,
                    "skills": skills,
                    "elapsed_ms": elapsed_ms,
                    "prompt_tokens": token_cb.prompt_tokens,
                    "completion_tokens": token_cb.completion_tokens,
                    "total_tokens": token_cb.total_tokens,
                },
            )
        # span 导出（进程内收集，非外部导出）：OTel 元信息回填 trace，供前端展示 trace_id / 节点时间线
        spans = round_tracer.finish() if round_tracer is not None else []
        otel_info = otel_meta(round_tracer, spans)
        if otel_info.get("trace_id"):
            trace["otel"] = otel_info
        # 轮级观测指标回填 node_trace：前端「每条对话的观测信息」与历史消息还原共用
        trace["elapsed_ms"] = elapsed_ms
        if token_cb.total_tokens:
            trace["prompt_tokens"] = token_cb.prompt_tokens
            trace["completion_tokens"] = token_cb.completion_tokens
            trace["total_tokens"] = token_cb.total_tokens

        full_trace = {
            **trace,
            "final_answer": final_answer,
            "intent": intent,
            "skills": skills,
            "multi_query": multi_query,
            "mock_mode": mock_mode,
            "elapsed_ms": elapsed_ms,
            # 本轮 token 消耗（LLM 未接入时为 None），供前端对话内观测信息展示
            "prompt_tokens": token_cb.prompt_tokens or None,
            "completion_tokens": token_cb.completion_tokens or None,
            "total_tokens": token_cb.total_tokens or None,
        }

        saved = await self._message_repo.add(
            MessageData(
                id="",
                session_id=session.id,
                role="assistant",
                content=final_answer,
                node_trace=trace,
            )
        )

        # span 落库（观测主链路）：round_id 取助手消息 id（与 trace_records.round_id 对齐）。
        # 独立于对话状态快照，未装配 conv_state_repo 时也必须执行；失败仅告警、不影响主链路。
        if spans and self._span_repo is not None:
            for item in spans:
                item.session_id = session.id
                item.round_id = saved.id
            try:
                await self._span_repo.add_many(spans)
            except Exception as exc:  # noqa: BLE001 - 观测落库失败不影响主链路
                logger.warning("span 落库失败：%s", exc)

        # P0-6 对话状态快照落库：本轮成功查询后，把已解析实体写回 conversation_states，
        # 供下一轮注入 context_entities 做确定性指代消解。失败仅告警、不影响回报。
        if self._conv_state_repo is not None and not trace.get("error"):
            try:
                from dmf_agent.services.conversation_state import ConversationStateService

                # 节点产出的字段名是 metrics/dimensions/filters（filters 为条件对象列表），
                # 对话状态表用 active_* 命名（filters 归一为 {字段: 值} 便于下轮确定性兜底）。
                qe = collected.get("query_elements") or {}
                norm_elements: Dict[str, Any] = {
                    "active_metrics": qe.get("metrics") or [],
                    "active_dimensions": qe.get("dimensions") or [],
                    "active_filters": _filters_to_map(qe.get("filters")),
                    "time_range": qe.get("time_range") or {},
                    "topic": qe.get("topic") or "",
                }
                svc = ConversationStateService(self._conv_state_repo)
                await svc.snapshot_from(
                    session_id=session.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    intent=intent or "",
                    query_elements=norm_elements,
                    query_original=collected.get("query_original") or message,
                    query_rewritten=collected.get("query_rewritten") or rewritten_query,
                    confirmation=collected.get("confirmation"),
                )
            except Exception as exc:  # noqa: BLE001 - 状态落库失败不影响主链路
                logger.warning("对话状态快照落库失败（忽略）：%s", exc)

        trace_saved_at = None
        if self._trace_repo is not None:
            try:
                # FR11 成本计价：按本轮实际用量 + 模型单价估算（纯本地计算，失败一律记 0）
                cost_info = estimate_cost(
                    _llm_model(self._settings),
                    token_cb.prompt_tokens,
                    token_cb.completion_tokens,
                    settings=self._settings,
                )
                saved_record = await self._trace_repo.add(
                    TraceData(
                        session_id=session.id,
                        round_id=saved.id,
                        intent=intent,
                        mock_mode=mock_mode,
                        skills=skills,
                        elapsed_ms=elapsed_ms,
                        has_error=bool(trace.get("error")),
                        prompt_tokens=token_cb.prompt_tokens or None,
                        completion_tokens=token_cb.completion_tokens or None,
                        total_tokens=token_cb.total_tokens or None,
                        user_id=user_id or "",
                        tenant_id=tenant_id or "",
                        # mock 轮没有真实 LLM 调用，不记模型（成本亦为 0）
                        model="" if mock_mode else str(cost_info.get("model") or ""),
                        cost=float(cost_info.get("cost") or 0.0),
                        trace=full_trace,
                    )
                )
                # 内存 / SQL 实现 add 后均回写 created_at，复用为留痕文件的 saved_at
                trace_saved_at = saved_record.created_at
            except Exception as exc:  # noqa: BLE001 - 观测落库失败不影响主链路
                logger.warning("trace 落库失败：%s", exc)

        # 合规留痕：每轮 trace 额外落盘到可配置目录（best-effort，失败仅告警不阻断）
        if self._trace_archiver is not None:
            try:
                await self._trace_archiver.save(
                    session.id, saved.id, full_trace, saved_at=trace_saved_at
                )
            except Exception as exc:  # noqa: BLE001 - 留痕落盘失败不影响主链路
                logger.warning("trace 留痕落盘失败：%s", exc)

        # FR13/P2-6 审计：把本轮 token 消耗并入审计明细（与 data_access 按 round_id 关联）。
        # 仅真实 LLM 调用（token_cb.total_tokens>0）才记；mock 轮无消耗不记。
        if self._security is not None and token_cb.total_tokens:
            try:
                usage_cost_info = estimate_cost(
                    _llm_model(self._settings),
                    token_cb.prompt_tokens,
                    token_cb.completion_tokens,
                    settings=self._settings,
                )
                await self._security.audit_token_usage(
                    session_id=session.id,
                    user_id=user_id or "",
                    tenant_id=tenant_id or "",
                    round_id=saved.id,
                    prompt_tokens=token_cb.prompt_tokens,
                    completion_tokens=token_cb.completion_tokens,
                    total_tokens=token_cb.total_tokens,
                    model=str(usage_cost_info.get("model") or ""),
                    cost=float(usage_cost_info.get("cost") or 0.0),
                    intent=intent or "",
                )
            except Exception:  # noqa: BLE001 - 审计失败不阻断主链路
                logger.warning("token 审计写入失败（跳过）", exc_info=True)

        yield {
            "type": "final",
            "session_id": session.id,
            "message_id": saved.id,
            # 本轮用户提问的落库 id：前端「编辑重发 → 替换该轮」用它作为裁剪锚点
            "user_message_id": user_saved.id,
            "content": final_answer,
            "intent": intent,
            "skills": skills,
            "multi_query": multi_query,
            "rewritten_query": rewritten_query,
            "mock_mode": mock_mode,
            # 结构化澄清表单（WorkBuddy 式确认）：前端据此渲染指标/维度/时间选择卡片，
            # 用户提交后带着选择再次调用本接口继续执行后续步骤
            "clarification": (trace.get("confirmation") or {}).get("clarification"),
            "node_trace": trace,  # 前端据此渲染过程与数据图表（query_result 已限行）
        }

    @staticmethod
    def _handle_debug_event(
        payload: Any, round_tracer: Any, open_node_spans: Dict[str, Any]
    ) -> None:
        """把 LangGraph debug 事件转成 OTel 节点子 span。

        - `task`（节点开始）：按事件 timestamp 作为 span 开始时间开启子 span；
        - `task_result`（节点结束）：按事件 timestamp 结束 span，并按 error 结果标记状态。
        """
        if round_tracer is None or not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        body = payload.get("payload")
        if not isinstance(body, dict):
            return
        node_name = body.get("name")
        key = str(body.get("id") or node_name or "")
        if not key or not node_name:
            return

        if event_type == "task":
            open_node_spans[key] = round_tracer.start_node(
                node_name,
                {"langgraph.step": payload.get("step")},
                start_time_ns=iso_to_ns(payload.get("timestamp")),
            )
        elif event_type == "task_result":
            handle = open_node_spans.pop(key, None)
            if handle is None:
                return
            error = body.get("error")
            handle.end(
                error=bool(error),
                message=str(error or ""),
                end_time_ns=iso_to_ns(payload.get("timestamp")),
            )
