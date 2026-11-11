"""会话管理接口（对应老系统 sa_meta_server.py 的会话 CRUD）。

多租户隔离：鉴权开启时，会话归属当前登录用户（user_id）与所属租户（tenant_id），
列表按 tenant_id 过滤（跨租户不可见）；鉴权关闭（测试/本地）时回退按 user_id 过滤。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.session import (
    ConversationStateData,
    MessageData,
    SessionCreate,
    SessionData,
    SessionRename,
)
from dmf_agent.services.conversation_state import ConversationStateService
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()


def _scope(runtime: AppRuntime, current: CurrentUser, req_user_id: str) -> tuple[str, str]:
    """返回 (归属用户, 归属租户)：鉴权开启用令牌身份，关闭时回退请求参数（tenant 为空）。"""
    if runtime.settings.auth.enabled:
        return current.id, current.tenant_id
    return req_user_id, ""


async def _assert_session_in_scope(
    runtime: AppRuntime, current: CurrentUser, session_id: str
) -> SessionData:
    session = await runtime.db.session_repo.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 鉴权开启时校验租户归属，跨租户按不存在处理
    if runtime.settings.auth.enabled and session.tenant_id != current.tenant_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/sessions", response_model=SessionData, summary="新建会话")
async def create_session(
    body: SessionCreate,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SessionData:
    user_id, tenant_id = _scope(runtime, current, body.user_id)
    return await runtime.db.session_repo.create(
        user_id=user_id, title=body.title, tenant_id=tenant_id
    )


@router.get("/sessions", response_model=List[SessionData], summary="用户会话列表（按租户隔离）")
async def list_sessions(
    user_id: str = "anonymous",
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[SessionData]:
    if runtime.settings.auth.enabled:
        return await runtime.db.session_repo.list_by_tenant(current.tenant_id)
    return await runtime.db.session_repo.list_by_user(user_id)


@router.get("/sessions/{session_id}", response_model=SessionData, summary="会话详情")
async def get_session(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SessionData:
    return await _assert_session_in_scope(runtime, current, session_id)


@router.patch("/sessions/{session_id}", response_model=SessionData, summary="重命名会话")
async def rename_session(
    session_id: str,
    body: SessionRename,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SessionData:
    await _assert_session_in_scope(runtime, current, session_id)
    session = await runtime.db.session_repo.rename(session_id, body.title)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.delete("/sessions/{session_id}", summary="删除会话（含消息）")
async def delete_session(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _assert_session_in_scope(runtime, current, session_id)
    await runtime.db.message_repo.delete_by_session(session_id)
    deleted = await runtime.db.session_repo.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}


@router.get(
    "/sessions/{session_id}/messages",
    response_model=List[MessageData],
    summary="会话历史消息",
)
async def list_messages(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[MessageData]:
    await _assert_session_in_scope(runtime, current, session_id)
    return await runtime.db.message_repo.list_by_session(session_id)


@router.delete(
    "/sessions/{session_id}/messages/after/{message_id}",
    summary="删除某条消息及其之后的全部消息（编辑重发的「替换该轮」）",
)
async def delete_messages_after(
    session_id: str,
    message_id: str,
    inclusive: bool = True,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """把 ``message_id``（含）及其之后的全部消息整段删除，用于「编辑重发 → 替换该轮」。

    默认追加式编辑（原对话保留）。当用户明确选择「替换该轮」时调本接口：先裁掉被改写的
    那条用户消息及其后续轮次，再以新内容重发，避免旧问答继续留在上下文里。

    ``inclusive=False`` 保留锚点本身、只删其后——用于「重试最后一轮」这类"保留提问、重出回答"。
    顺带重置该会话的结构化对话状态：消息被裁掉后旧状态（active_metrics / time_range 等）
    已与历史不符，若不清会成为下一轮的脏上下文（下一轮会重新快照）。
    """
    await _assert_session_in_scope(runtime, current, session_id)
    msg = await runtime.db.message_repo.get(message_id)
    if msg is None or msg.session_id != session_id:
        raise HTTPException(status_code=404, detail="消息不存在")
    removed = await runtime.db.message_repo.delete_after(
        session_id, message_id, inclusive=inclusive
    )
    state_reset = False
    if runtime.db.conversation_state_repo is not None:
        state_reset = await ConversationStateService(runtime.db.conversation_state_repo).reset(
            session_id
        )
    return {"deleted": removed, "state_reset": state_reset}


@router.get(
    "/sessions/{session_id}/state",
    response_model=Optional[ConversationStateData],
    summary="对话状态（P0-6 多轮上下文）",
)
async def get_conversation_state(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Optional[ConversationStateData]:
    """返回该会话的结构化对话状态：最近一轮已解析实体（指标 / 维度 / 过滤 / 时间窗）、
    主题、累计轮次与状态机状态（active / awaiting_clarification / idle）。

    用于前端展示「当前对话上下文」与多轮追问的确定性指代消解来源。
    复用会话归属校验：跨租户 / 不存在按 404 处理。
    """
    await _assert_session_in_scope(runtime, current, session_id)
    if runtime.db.conversation_state_repo is None:
        return None
    return await ConversationStateService(runtime.db.conversation_state_repo).get(session_id)


@router.post(
    "/sessions/{session_id}/state/reset",
    summary="重置对话状态（清空多轮上下文）",
)
async def reset_conversation_state(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """清空该会话的结构化对话状态（用户主动「重置上下文」）。

    仅清空 `conversation_states`，不影响消息历史与记忆摘要；下一轮起退化为无上下文，
    重新从本轮问句解析实体。复用会话归属校验。
    """
    await _assert_session_in_scope(runtime, current, session_id)
    if runtime.db.conversation_state_repo is None:
        return {"reset": False, "reason": "对话状态服务未启用"}
    existed = await ConversationStateService(runtime.db.conversation_state_repo).reset(
        session_id
    )
    return {"reset": True, "existed": existed}

