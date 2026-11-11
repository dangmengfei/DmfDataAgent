"""对话接口：阻塞 JSON + SSE 流式。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.chat import ChatRequest, ChatResponse
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter()


def _identity(runtime: AppRuntime, current: CurrentUser, req: ChatRequest) -> tuple[str, str]:
    """返回 (user_id, tenant_id)：鉴权开启用令牌身份，关闭时回退请求体（tenant 为空）。"""
    if runtime.settings.auth.enabled:
        return current.id, current.tenant_id
    return req.user_id, ""


def _authz(runtime: AppRuntime, current: CurrentUser) -> tuple[str, list[str]]:
    """返回 (role, permissions)：鉴权开启用令牌身份；关闭时按 viewer 兜底（Harness 仍放行）。"""
    if runtime.settings.auth.enabled:
        return current.role, list(current.permissions or [])
    # 未开启鉴权（本地/测试）：给通配权限，避免 Harness 工具级校验误杀取数
    return "viewer", ["*"]


@router.post("/chat", response_model=ChatResponse, summary="对话（阻塞式）")
async def chat(
    req: ChatRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> ChatResponse:
    user_id, tenant_id = _identity(runtime, current, req)
    role, permissions = _authz(runtime, current)
    result = await runtime.chat_service.chat(
        session_id=req.session_id, user_id=user_id, message=req.message, tenant_id=tenant_id,
        user_role=role, user_permissions=permissions,
        clarify_selection=req.clarify_selection,
    )
    return ChatResponse(**result)


@router.post("/chat/stream", summary="对话（SSE 流式）")
async def chat_stream(
    req: ChatRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> StreamingResponse:
    user_id, tenant_id = _identity(runtime, current, req)
    role, permissions = _authz(runtime, current)

    async def event_source():
        try:
            async for event in runtime.chat_service.astream_chat(
                session_id=req.session_id, user_id=user_id, message=req.message, tenant_id=tenant_id,
                user_role=role, user_permissions=permissions,
                # 结构化澄清提交：确定性合并要素后自动续跑后续步骤
                clarify_selection=req.clarify_selection,
            ):
                yield _format_sse(event)
        except Exception as exc:  # noqa: BLE001 - 流中断统一回报
            logger.exception("SSE 流式对话异常")
            yield _format_sse({"type": "error", "message": f"服务内部错误：{exc}"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format_sse(event: dict) -> str:
    """SSE 帧格式：event: <类型>\\ndata: <json>\\n\\n

    事件类型与老系统（sse_handler 的 post_update_and_send 等）的映射见 MIGRATION.md。
    """
    event_type = event.pop("type", "message")
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
