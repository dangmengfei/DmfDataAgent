"""合规审计日志只读接口（PRD FR13：全链路审计可查）。

数据源 `audit_logs` 表（迁移 0014），由 Harness / 安全层 / 数据访问事件写入。
仅 admin 可查（含越权尝试、拦截、数据访问明细等敏感信息）。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_permission
from dmf_agent.auth.roles import PERM_ADMIN
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])

_admin = [Depends(require_permission(PERM_ADMIN))]


@router.get("/logs", summary="审计日志查询（分页/过滤）", dependencies=_admin)
async def list_audit_logs(
    event_type: Optional[str] = Query(None, description="事件类型，如 data_access / security/input_guard"),
    level: Optional[str] = Query(None, description="级别 info/warning/high"),
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    repo = runtime.db.audit_log_repo
    offset = (page - 1) * page_size
    items, total = await repo.query(
        event_type=event_type,
        level=level,
        session_id=session_id,
        user_id=user_id,
        limit=page_size,
        offset=offset,
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats", summary="审计事件统计（按类型/级别）", dependencies=_admin)
async def audit_stats(runtime: AppRuntime = Depends(get_runtime)) -> dict:
    return await runtime.db.audit_log_repo.stats()
