"""记忆分层管理 API（PRD FR9 管理面 / PRD P2-4 记忆管理页）。

L0–L4 分层记忆的用户侧与平台侧管理入口。接口形状对齐
`doc/记忆分层改造方案.md` §5.4（按层组织：`/memory/l1|l2|l3`）。

**L1 会话记忆**
- `GET    /memory/l1`                     摘要列表（跨会话浏览，管理页用）
- `GET    /memory/l1/{session_id}`        单会话摘要（本人维度）
- `POST   /memory/session/{id}/clear`     清会话记忆（MySQL + Redis 热层）
- `GET    /memory/session/{id}/context`   上下文占比状态（轮次口径，前端圆环）
- `POST   /memory/session/{id}/compress`  手动把更早轮次压缩为会话摘要

**记忆开关（用户级）**
- `POST   /memory/disable`                一键关闭 / 开启记忆注入
- `GET    /memory/disable`                查询开关状态

**L2 用户偏好**
- `GET    /memory/preferences`            本人偏好列表
- `PUT    /memory/preferences`            写入 / 覆盖一条本人偏好
- `DELETE /memory/preferences/{key}`      删除本人一条偏好
- `DELETE /memory/preferences`            清空本人全部偏好
- `GET    /memory/l2`                     全平台偏好列表（平台视角，需 memory 权限）
- `DELETE /memory/l2/{pref_id}`           跨用户删除一条偏好（需 memory:write）

**L3 领域知识**
- `GET    /memory/l3`                     列表（domain / type / status / keyword 过滤 + 分页）
- `POST   /memory/l3`                     录入一条（需 memory:write）
- `GET    /memory/l3/{item_id}`           详情
- `PUT    /memory/l3/{item_id}`           编辑 / 审核（改 status 即通过或驳回；需 memory:write）
- `DELETE /memory/l3/{item_id}`           删除（需 memory:write）

**统计**
- `GET    /memory/stats`                  各层条数（管理页统计卡）

说明：
- **身份绑定**：`disable` / `preferences`（本人接口）/ 清会话一律以**令牌身份**为准，
  不接受调用方传入他人 user_id（避免越权关掉别人记忆或读别人偏好）；
  仅在鉴权关闭（合成 anonymous 用户，本地调试）时才接受显式 user_id 参数。
- **平台视角**（`/l1` `/l2` `/l3` `/stats`）需 `memory` 权限点，写侧需 `memory:write`；
  viewer 无这些权限点，接口级 403 + 前端入口不渲染（双重）。
- L1 清会话同时清 MySQL 主存与 Redis 热层（仓储 delete 已双写）。
- 一键关闭开关为内存态（重启失效），写入 runtime.memory_disabled[user_id]；
  chat_service.astream_chat 在开启时跳过记忆装配（L1 历史装配 + L2 偏好注入同步失效）。
- L4 组织规范当前版本未建表，相关 API 留待后续阶段。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import (
    PERM_CHAT,
    PERM_MEMORY,
    PERM_MEMORY_WRITE,
)
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.memory import (
    DomainMemoryData,
    DomainMemoryPatch,
    DomainMemoryUpsert,
    ManagedSummaryData,
    UserPreferenceData,
    UserPreferenceUpsert,
    UserPreferenceView,
)
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]


# 管理页统计卡最多统计的会话数（与 SessionRepository.list_all 的默认上限一致）
_SESSION_SCAN_LIMIT = 500


def _effective_user_id(current: CurrentUser, explicit: Optional[str] = None) -> str:
    """解析本次操作的目标 user_id。

    鉴权开启：一律用令牌身份，忽略传入值（防越权）；
    鉴权关闭（合成 anonymous）：允许显式指定，保持本地调试与既有调用兼容。
    """
    if current.id and current.id != "anonymous":
        if explicit and explicit != current.id:
            logger.warning(
                "忽略越权 user_id=%s，改用令牌身份=%s", explicit, current.id
            )
        return current.id
    return explicit or current.id or "anonymous"


async def _assert_session_owned(
    session_id: str, current: CurrentUser, runtime: AppRuntime
) -> None:
    """会话归属校验：跨租户/跨用户按 404（不泄露存在性）；鉴权关闭时不校验。"""
    if not runtime.settings.auth.enabled:
        return
    session = await runtime.db.session_repo.get(session_id)
    if session is None:
        return  # 无该会话：清记忆按幂等处理，不报错
    if current.tenant_id and session.tenant_id not in ("", current.tenant_id):
        raise HTTPException(status_code=404, detail="会话不存在")


async def _session_index(runtime: AppRuntime) -> Dict[str, object]:
    """会话 id -> SessionData 索引（一次查询，供 L1 列表补标题 / 归属，避免 N+1）。"""
    try:
        sessions = await runtime.db.session_repo.list_all(limit=_SESSION_SCAN_LIMIT)
    except Exception as exc:  # noqa: BLE001 - 补标题失败不该让记忆列表整体 500
        logger.warning("加载会话索引失败（L1 列表将缺少标题）：%s", exc)
        return {}
    return {s.id: s for s in sessions}


# ---------- L1 会话记忆 ----------


@router.get(
    "/l1",
    response_model=List[ManagedSummaryData],
    dependencies=_guard(PERM_MEMORY),
    summary="L1 会话摘要列表（跨会话，管理页用）",
)
async def list_l1(
    keyword: str = Query("", description="按摘要正文过滤（大小写不敏感）"),
    limit: int = Query(100, ge=1, le=500),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[ManagedSummaryData]:
    """跨会话浏览"都有哪些会话记忆"（原本只能按 session_id 单条查）。

    非 admin 仅能看到**本租户**会话的摘要（多租户隔离）；摘要标题从会话表补齐。
    """
    summaries = await runtime.db.memory_repo.list_recent(limit=limit, keyword=keyword)
    sessions = await _session_index(runtime)
    out: List[ManagedSummaryData] = []
    for s in summaries:
        obj = sessions.get(s.session_id)
        tenant_id = getattr(obj, "tenant_id", "") if obj is not None else ""
        # 租户隔离：非管理员只读本租户（会话已被删除 / 索引缺失时按可见处理，避免记忆"凭空消失"）
        if obj is not None and current.tenant_id and tenant_id not in ("", current.tenant_id):
            continue
        out.append(
            ManagedSummaryData(
                session_id=s.session_id,
                session_title=getattr(obj, "title", "") if obj is not None else "",
                user_id=getattr(obj, "user_id", "") if obj is not None else "",
                summary=s.summary,
                upto_message_id=s.upto_message_id,
                updated_at=s.updated_at,
                last_accessed_at=s.last_accessed_at,
                expires_at=s.expires_at,
            )
        )
    return out


@router.get(
    "/l1/{session_id}",
    dependencies=_guard(PERM_CHAT),
    summary="查看 L1 会话摘要",
)
async def get_l1(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _assert_session_owned(session_id, current, runtime)
    summary = await runtime.db.memory_repo.get(session_id)
    if summary is None:
        return {"session_id": session_id, "summary": None}
    return {
        "session_id": summary.session_id,
        "summary": summary.summary,
        "upto_message_id": summary.upto_message_id,
        "updated_at": summary.updated_at.isoformat() if summary.updated_at else None,
        "expires_at": summary.expires_at.isoformat() if summary.expires_at else None,
    }


@router.post(
    "/session/{session_id}/clear",
    dependencies=_guard(PERM_CHAT),
    summary="清 L1 会话记忆（MySQL + Redis 热层）",
)
async def clear_session_memory(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _assert_session_owned(session_id, current, runtime)
    try:
        await runtime.db.memory_repo.delete(session_id)
    except Exception as exc:  # noqa: BLE001 - 清记忆失败要明确返回，不静默
        logger.warning("清 L1 会话记忆失败 session=%s：%s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"清会话记忆失败：{exc}")
    logger.info("已清 L1 会话记忆：session=%s", session_id)
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="clear_session", layer="L1",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=session_id, session_id=session_id,
            summary=f"清空会话记忆 {session_id}",
        )
    return {"ok": True, "session_id": session_id, "cleared": "l1"}


@router.get(
    "/session/{session_id}/context",
    dependencies=_guard(PERM_CHAT),
    summary="上下文占比状态（轮次口径，前端圆环）",
)
async def session_context_status(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _assert_session_owned(session_id, current, runtime)
    return await runtime.chat_service.history_context_status(session_id)


@router.post(
    "/session/{session_id}/compress",
    dependencies=_guard(PERM_CHAT),
    summary="手动把更早轮次压缩为会话摘要（保留最近 retain 轮）",
)
async def compress_session_context(
    session_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _assert_session_owned(session_id, current, runtime)
    result = await runtime.chat_service.compress_history(session_id)
    if result.get("reason") in ("summarize_failed",):
        raise HTTPException(status_code=500, detail="上下文压缩失败，请稍后重试")
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="compress", layer="L1",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=session_id, session_id=session_id,
            summary=f"压缩会话更早轮次为摘要 {session_id}",
        )
    return {"ok": True, "session_id": session_id, **result}


# ---------- 记忆开关（用户级）----------


@router.post("/disable", dependencies=_guard(PERM_CHAT), summary="一键关闭/开启记忆注入")
async def set_memory_disabled(
    body: dict,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """一键关闭 / 开启记忆注入（L1 历史装配 + L2 偏好注入一并生效）。

    body: {disabled, user_id?}；user_id 仅在鉴权关闭时可用（见 _effective_user_id）。
    """
    user_id = _effective_user_id(current, body.get("user_id"))
    disabled = bool(body.get("disabled", True))
    if disabled:
        runtime.memory_disabled[user_id] = True
    else:
        runtime.memory_disabled.pop(user_id, None)
    logger.info("记忆注入开关：user=%s disabled=%s", user_id, disabled)
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="disable" if disabled else "enable", layer="ALL",
            user_id=user_id, tenant_id=current.tenant_id or "",
            target=user_id, detail={"disabled": disabled},
            summary=("关闭" if disabled else "开启") + f"记忆注入：user={user_id}",
        )
    return {"ok": True, "user_id": user_id, "disabled": disabled}


@router.get("/disable", dependencies=_guard(PERM_CHAT), summary="查询记忆注入开关")
async def get_memory_disabled(
    user_id: Optional[str] = Query(None, description="仅鉴权关闭时可指定；否则取令牌身份"),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    target = _effective_user_id(current, user_id)
    return {"user_id": target, "disabled": bool(runtime.memory_disabled.get(target))}


# ---------- L2 用户偏好 ----------


@router.get(
    "/preferences",
    response_model=List[UserPreferenceData],
    dependencies=_guard(PERM_CHAT),
    summary="L2 用户偏好列表（默认本人）",
)
async def list_preferences(
    status: Optional[str] = Query(None, pattern="^(active|archived)$"),
    user_id: Optional[str] = Query(None, description="查看他人需 memory 权限（analyst+）"),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[UserPreferenceData]:
    target = current.id or "anonymous"
    if user_id and user_id != target:
        # 查看他人偏好属平台视角：要求 memory 权限点
        allowed = {"*", PERM_MEMORY}
        if not (set(current.permissions) & allowed):
            raise HTTPException(status_code=403, detail="权限不足：无法查看其他用户的偏好")
        target = user_id
    return await runtime.db.preference_repo.list_by_user(target, status=status)


@router.put(
    "/preferences",
    response_model=UserPreferenceData,
    dependencies=_guard(PERM_CHAT),
    summary="写入 / 覆盖一条 L2 偏好",
)
async def upsert_preference(
    body: UserPreferenceUpsert,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> UserPreferenceData:
    saved_pref = await runtime.db.preference_repo.upsert(
        UserPreferenceData(
            id=0,
            user_id=current.id or "anonymous",
            tenant_id=current.tenant_id or "",
            key=body.key,
            value=body.value,
            source=body.source,
            confidence=body.confidence,
            status=body.status,
        )
    )
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="upsert", layer="L2",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=body.key, detail={"key": body.key, "pref_id": saved_pref.id},
            summary=f"写入/覆盖偏好 {body.key}",
        )
    return saved_pref


@router.delete(
    "/preferences/{key}",
    dependencies=_guard(PERM_CHAT),
    summary="删除一条 L2 偏好",
)
async def delete_preference(
    key: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    deleted = await runtime.db.preference_repo.delete(current.id or "anonymous", key)
    if not deleted:
        raise HTTPException(status_code=404, detail="偏好不存在")
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="delete", layer="L2",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=key, detail={"key": key}, summary=f"删除本人偏好 {key}",
        )
    return {"deleted": True, "key": key}


@router.delete(
    "/preferences",
    dependencies=_guard(PERM_CHAT),
    summary="清空本人全部 L2 偏好",
)
async def clear_preferences(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    removed = await runtime.db.preference_repo.clear_user(current.id or "anonymous")
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="clear_all", layer="L2",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=current.id or "anonymous", detail={"removed": removed},
            summary=f"清空本人全部偏好（{removed} 条）",
        )
    return {"removed": removed}


@router.get(
    "/l2",
    response_model=List[UserPreferenceView],
    dependencies=_guard(PERM_MEMORY),
    summary="L2 全平台偏好列表（管理面平台视角）",
)
async def list_l2(
    tenant_id: Optional[str] = Query(None, description="按租户过滤"),
    user_id: Optional[str] = Query(None, description="按用户过滤"),
    source: Optional[str] = Query(None, pattern="^(user|inferred)$"),
    status: Optional[str] = Query(None, pattern="^(active|archived)$"),
    limit: int = Query(200, ge=1, le=1000),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[UserPreferenceView]:
    """管理面 L2 视角：跨用户查看偏好（"这条系统学到的粒度是谁的"）。

    非 admin 强制收敛到本租户，避免跨租户读到他人偏好。
    """
    scoped_tenant = tenant_id
    if current.tenant_id and not (set(current.permissions) & {"*"}):
        scoped_tenant = current.tenant_id
    items = await runtime.db.preference_repo.list_all(
        tenant_id=scoped_tenant, user_id=user_id, source=source,
        status=status, limit=limit,
    )
    labels: Dict[str, tuple] = {}
    try:
        for u in await runtime.db.user_repo.list_all():
            labels[u.id] = (u.username, u.display_name or "")
    except Exception as exc:  # noqa: BLE001 - 补用户名失败不阻断列表
        logger.warning("加载用户索引失败（L2 列表将缺少用户名）：%s", exc)
    out: List[UserPreferenceView] = []
    for it in items:
        username, display_name = labels.get(it.user_id, ("", ""))
        out.append(
            UserPreferenceView(
                **it.model_dump(), username=username, display_name=display_name
            )
        )
    return out


@router.delete(
    "/l2/{pref_id}",
    dependencies=_guard(PERM_MEMORY_WRITE),
    summary="删除任意用户的一条 L2 偏好（管理面）",
)
async def delete_l2_by_id(
    pref_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """跨用户删除（管理动作，需 memory:write）。

    与 `DELETE /preferences/{key}`（本人按 key 删）区分：这里按主键删，
    因为管理页列表里同一 key 会出现在不同用户名下，按 key 删会删错人。
    非 admin 只允许删本租户的记录。
    """
    items = await runtime.db.preference_repo.list_all(limit=1000)
    target = next((p for p in items if p.id == pref_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="偏好不存在")
    if (
        current.tenant_id
        and not (set(current.permissions) & {"*"})
        and target.tenant_id not in ("", current.tenant_id)
    ):
        raise HTTPException(status_code=404, detail="偏好不存在")
    await runtime.db.preference_repo.delete_by_id(pref_id)
    logger.info("管理面删除 L2 偏好：id=%s user=%s key=%s", pref_id, target.user_id, target.key)
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="admin_delete", layer="L2",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=target.key,
            detail={"pref_id": pref_id, "owner_user_id": target.user_id, "key": target.key},
            summary=f"管理面删除用户 {target.user_id} 的偏好 {target.key}",
        )
    return {"deleted": True, "id": pref_id}


# ---------- L3 领域知识 ----------


@router.get(
    "/l3",
    response_model=List[DomainMemoryData],
    dependencies=_guard(PERM_MEMORY),
    summary="L3 领域知识列表（过滤 + 分页）",
)
async def list_l3(
    domain: Optional[str] = Query(None),
    type: Optional[str] = Query(None, description="few_shot|rule|glossary|correction"),
    status: Optional[str] = Query(None, description="pending|approved|rejected"),
    keyword: str = Query("", description="匹配问题 / 答案 / 标签"),
    include_few_shot: bool = Query(
        True,
        description="是否包含 type=few_shot 的点赞样本镜像（样本维护请用「Few-Shot 管理」页）",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[DomainMemoryData]:
    """L3 领域知识（规则 / 术语 / 纠错 + 点赞沉淀的 few_shot 镜像）。

    默认不过滤 status（管理页要能看待审的 pending）；`include_few_shot=false`
    时排除点赞镜像，只留"管理员维护型"知识。
    """
    repo = runtime.db.domain_memory_repo
    try:
        items, _total = await repo.query(
            domain=domain, type=type, status=status, keyword=keyword,
            limit=limit, offset=offset,
        )
    except Exception as exc:  # noqa: BLE001 - 查询失败降级为空列表
        logger.warning("查询 L3 领域记忆失败：%s", exc)
        return []
    if not include_few_shot:
        items = [it for it in items if it.type != "few_shot"]
    return items


@router.post(
    "/l3",
    response_model=DomainMemoryData,
    dependencies=_guard(PERM_MEMORY_WRITE),
    summary="录入一条 L3 领域知识",
)
async def create_l3(
    body: DomainMemoryUpsert,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> DomainMemoryData:
    created = await runtime.db.domain_memory_repo.add(
        DomainMemoryData(
            id=0,
            domain=body.domain,
            type=body.type,
            question=body.question,
            answer=body.answer,
            intent=body.intent,
            tags=body.tags,
            weight=body.weight,
            status=body.status,
            created_by=current.username or current.id or "system",
        )
    )
    logger.info("管理面新增 L3 记忆：id=%s type=%s", created.id, created.type)
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="create", layer="L3",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=str(created.id), detail={"item_id": created.id, "type": created.type, "domain": created.domain},
            summary=f"新增 L3 领域知识（{created.type}）",
        )
    return created


@router.get(
    "/l3/{item_id}",
    response_model=DomainMemoryData,
    dependencies=_guard(PERM_MEMORY),
    summary="L3 领域知识详情",
)
async def get_l3(
    item_id: int,
    runtime: AppRuntime = Depends(get_runtime),
) -> DomainMemoryData:
    item = await runtime.db.domain_memory_repo.get_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="领域记忆不存在")
    return item


@router.put(
    "/l3/{item_id}",
    response_model=DomainMemoryData,
    dependencies=_guard(PERM_MEMORY_WRITE),
    summary="编辑 / 审核一条 L3 领域知识",
)
async def update_l3(
    item_id: int,
    body: DomainMemoryPatch,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> DomainMemoryData:
    """编辑内容或审核（改 `status`：approved 生效 / rejected 驳回）。"""
    updated = await runtime.db.domain_memory_repo.update(
        item_id, **body.model_dump(exclude_none=True)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="领域记忆不存在")
    logger.info(
        "管理面更新 L3 记忆：id=%s status=%s", item_id, updated.status
    )
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="update", layer="L3",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=str(item_id), detail={"item_id": item_id, "status": updated.status},
            summary=f"更新/审核 L3 领域知识（status={updated.status}）",
        )
    return updated


@router.delete(
    "/l3/{item_id}",
    dependencies=_guard(PERM_MEMORY_WRITE),
    summary="删除一条 L3 领域知识",
)
async def delete_l3(
    item_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    if await runtime.db.domain_memory_repo.get_by_id(item_id) is None:
        raise HTTPException(status_code=404, detail="领域记忆不存在")
    await runtime.db.domain_memory_repo.delete(item_id)
    logger.info("管理面删除 L3 记忆：id=%s", item_id)
    if getattr(runtime, "security", None) is not None:
        await runtime.security.audit_memory_operation(
            operation="delete", layer="L3",
            user_id=current.id or "anonymous", tenant_id=current.tenant_id or "",
            target=str(item_id), detail={"item_id": item_id},
            summary=f"删除 L3 领域知识 id={item_id}",
        )
    return {"deleted": True, "id": item_id}


# ---------- 统计 ----------


@router.get("/stats", dependencies=_guard(PERM_MEMORY), summary="各层记忆统计（管理页统计卡）")
async def memory_stats(
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """各层条数概览；单层查询失败时该项返回 0（不让统计卡把整页拖垮）。"""
    db = runtime.db

    async def _safe(coro, default=0):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆统计失败（按 0 计）：%s", exc)
            return default

    l3_by_type = await _safe(db.domain_memory_repo.count_by_type(), {})
    # 待审数单独走一次带 status 过滤的计数（count_by_type 不区分状态）
    _l3, l3_total = await _safe(db.domain_memory_repo.query(limit=1), ([], 0))
    _pending, l3_pending = await _safe(
        db.domain_memory_repo.query(status="pending", limit=1), ([], 0)
    )
    few_total = await _safe(db.few_shot_repo.count())
    few_active = await _safe(db.few_shot_repo.count(status="active"))
    few_disabled = few_total - few_active if isinstance(few_total, int) else 0
    few_like = await _safe(db.few_shot_repo.count(source="like"))
    few_manual = await _safe(db.few_shot_repo.count(source="manual"))
    return {
        "l1": {"summaries": await _safe(db.memory_repo.count())},
        "l2": {"preferences": await _safe(db.preference_repo.count())},
        "l3": {
            "total": l3_total,
            "pending": l3_pending,
            "by_type": l3_by_type,
            # 去掉点赞镜像后的"管理员维护型"知识条数（rule + glossary + correction）
            "curated": sum(
                v for k, v in (l3_by_type or {}).items() if k != "few_shot"
            ),
        },
        "few_shot": {
            "total": few_total,
            "active": few_active,
            "disabled": few_disabled,
            "like": few_like,
            "manual": few_manual,
        },
    }
