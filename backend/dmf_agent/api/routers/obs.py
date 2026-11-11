"""运行观测 API（前端"运行观测"页 / 对话内观测信息的数据源）。

观测遵循 OpenTelemetry：
- `trace_records`：轮级汇总（每轮对话一条）；
- `trace_spans`：OTel span 明细（root `chat_round` + 各节点 `node.<name>`）。

接口分三组：
1. **兼容既有面板**（结构保持不变，仅做增量追加）：
   `GET /obs/summary`、`GET /obs/traces`、`GET /obs/traces/{session_id}/{round_id}`
2. **专页明细**：`GET /obs/rounds`（过滤 + 分页）、`GET /obs/spans`（节点性能汇总）
3. **会话维度 / 导出**：`GET /obs/sessions/{session_id}`（单个对话的观测汇总与明细）、
   `GET /obs/export`（导出 trace + span，支持 OTLP/JSON 与简版 JSON；不依赖外部平台）

所有数据均来自本地库（MySQL / 内存降级），**不接任何外部观测平台**。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_OBS
from dmf_agent.db.base import ErrorSampleRepository, SpanRepository, TraceRepository
from dmf_agent.observability.cost import budget_alerts
from dmf_agent.observability.quality import aggregate_quality
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.security.export_gate import (
    assert_export_allowed,
    audit_export,
    build_watermark,
    export_policy,
)
from dmf_agent.schemas.trace import SpanData, TraceData, aggregate_spans
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter()

def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]



def _repos(runtime: AppRuntime) -> tuple[TraceRepository, Optional[SpanRepository]]:
    return runtime.db.trace_repo, getattr(runtime.db, "span_repo", None)


def _error_repo(runtime: AppRuntime) -> Optional[ErrorSampleRepository]:
    return getattr(runtime.db, "error_sample_repo", None)


def _summary(r: TraceData) -> Dict[str, Any]:
    """轮级摘要（保持既有字段，追加 OTel 元信息）。"""
    return {
        "session_id": r.session_id,
        "round_id": r.round_id,
        "saved_at": r.created_at.isoformat() if r.created_at else None,
        "intent": r.intent,
        "mock_mode": r.mock_mode,
        "skills": r.skills,
        "elapsed_ms": r.elapsed_ms,
        "has_error": r.has_error,
        # token 消耗（LLM 未接入时为 None）
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "total_tokens": r.total_tokens,
        # ---- 追加：OTel 元信息（既有前端忽略这些字段，展示不变） ----
        "trace_id": r.trace_id,
        "span_count": r.span_count,
        "status": "error" if r.has_error else "ok",
        "question": (r.trace or {}).get("rewritten_query")
        or _first_user_question(r),
    }


def _first_user_question(r: TraceData) -> str:
    return str((r.trace or {}).get("question") or "")


def _span_detail(s: SpanData) -> Dict[str, Any]:
    return {
        "trace_id": s.trace_id,
        "span_id": s.span_id,
        "parent_span_id": s.parent_span_id,
        "name": s.name,
        "kind": s.kind,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "duration_ms": s.duration_ms,
        "status_code": s.status_code,
        "status_message": s.status_message,
        "attributes": s.attributes,
        "events": s.events,
        "service_name": s.service_name,
    }


# --------------------------------------------------------------------------
# 1. 兼容既有面板
# --------------------------------------------------------------------------


@router.get("/obs/summary", summary="观测汇总 KPI", dependencies=_guard(PERM_OBS))
async def obs_summary(
    session_id: Optional[str] = None,
    intent: Optional[str] = None,
    has_error: Optional[bool] = None,
    keyword: Optional[str] = None,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    trace_repo, _ = _repos(runtime)
    if session_id or intent or has_error is not None or keyword:
        return await trace_repo.aggregate_filtered(
            session_id=session_id,
            intent=intent,
            has_error=has_error,
            keyword=keyword,
        )
    return await trace_repo.aggregate()


@router.get("/obs/traces", summary="最近对话 trace 列表", dependencies=_guard(PERM_OBS))
async def obs_traces(
    limit: int = 50, runtime: AppRuntime = Depends(get_runtime)
) -> List[Dict[str, Any]]:
    trace_repo, _ = _repos(runtime)
    records = await trace_repo.list_recent(limit)
    return [_summary(r) for r in records]


@router.get("/obs/traces/{session_id}/{round_id}", summary="单轮 trace 详情", dependencies=_guard(PERM_OBS))
async def obs_trace_detail(
    session_id: str, round_id: str, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    trace_repo, span_repo = _repos(runtime)
    r = await trace_repo.get(session_id, round_id)
    if r is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    spans = await span_repo.list_by_round(session_id, round_id) if span_repo else []
    return {
        "session_id": r.session_id,
        "round_id": r.round_id,
        "saved_at": r.created_at.isoformat() if r.created_at else None,
        "trace": r.trace,
        # 追加：OTel span 明细（既有前端忽略；专页用于下钻）
        "otel": r.otel,
        "spans": [_span_detail(s) for s in spans],
    }


# --------------------------------------------------------------------------
# 2. 专页明细
# --------------------------------------------------------------------------


@router.get("/obs/rounds", summary="观测明细：轮次列表（过滤 + 分页）", dependencies=_guard(PERM_OBS))
async def obs_rounds(
    session_id: Optional[str] = None,
    intent: Optional[str] = None,
    has_error: Optional[bool] = None,
    keyword: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    trace_repo, _ = _repos(runtime)
    records, total = await trace_repo.query(
        session_id=session_id,
        intent=intent,
        has_error=has_error,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_summary(r) for r in records],
    }


@router.get("/obs/spans", summary="观测明细：节点性能汇总 / 最近 span", dependencies=_guard(PERM_OBS))
async def obs_spans(
    session_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    _, span_repo = _repos(runtime)
    if span_repo is None:
        return {"total": 0, "nodes": [], "spans": []}
    if session_id:
        spans = await span_repo.list_by_session(session_id, limit=limit)
    else:
        spans = await span_repo.list_recent(limit)
    agg = aggregate_spans(spans)
    return {**agg, "spans": [_span_detail(s) for s in spans[:limit]]}


# --------------------------------------------------------------------------
# 2.4 FR11 成本 / 质量看板
# --------------------------------------------------------------------------


def _day_start(days: int) -> Optional[datetime]:
    """N 天前（含今天）的零点；days<=0 表示不限时间窗。

    用**本地朴素时间**而非 UTC：`trace_records.created_at` 由 MySQL `NOW()` 生成，
    与时区转换后的 UTC 比较会整体偏移若干小时，导致"今天"的口径错误。
    """
    if not days or days <= 0:
        return None
    return datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days - 1)


@router.get("/obs/cost", summary="成本看板：按模型/用户/租户/意图/天归集 + 预算告警", dependencies=_guard(PERM_OBS))
async def obs_cost(
    group_by: str = Query("model", pattern="^(model|user|tenant|intent|day)$"),
    days: int = Query(7, ge=0, le=365),
    limit: int = Query(50, ge=1, le=500),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    trace_repo, _ = _repos(runtime)
    since = _day_start(days)
    items = await trace_repo.cost_breakdown(group_by=group_by, since=since, limit=limit)
    cost_settings = runtime.settings.cost
    total_cost = round(sum(float(i.get("cost") or 0.0) for i in items), 6)
    total_tokens = sum(int(i.get("total_tokens") or 0) for i in items)
    rounds = sum(int(i.get("rounds") or 0) for i in items)

    # 今日 / 本月累计（预算告警口径：不受 group_by 影响，独立按天聚合）
    today_items = await trace_repo.cost_breakdown(group_by="day", since=_day_start(1), limit=400)
    daily_cost = round(sum(float(i.get("cost") or 0.0) for i in today_items), 6)
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_items = await trace_repo.cost_breakdown(group_by="day", since=month_start, limit=400)
    monthly_cost = round(sum(float(i.get("cost") or 0.0) for i in month_items), 6)

    return {
        "currency": cost_settings.currency,
        "pricing_enabled": bool(cost_settings.enabled),
        "group_by": group_by,
        "days": days,
        "totals": {
            "cost": total_cost,
            "rounds": rounds,
            "total_tokens": total_tokens,
            "avg_cost_per_round": round(total_cost / rounds, 6) if rounds else 0.0,
        },
        "items": items,
        "budget": {
            "daily": {
                "limit": cost_settings.daily_budget,
                "used": daily_cost,
            },
            "monthly": {
                "limit": cost_settings.monthly_budget,
                "used": monthly_cost,
            },
            "alerts": budget_alerts(
                daily_cost=daily_cost,
                monthly_cost=monthly_cost,
                settings=runtime.settings,
            ),
        },
    }


@router.get("/obs/quality", summary="质量看板：有据回答率 / 采纳率 / 降级率 / 幻觉风险", dependencies=_guard(PERM_OBS))
async def obs_quality(
    days: int = Query(7, ge=0, le=365),
    limit: int = Query(2000, ge=1, le=10000),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    trace_repo, _ = _repos(runtime)
    since = _day_start(days)
    if since is not None:
        records = [
            r
            for r in await trace_repo.list_recent(limit)
            if r.created_at is None or _as_naive(r.created_at) >= since
        ]
    else:
        records = await trace_repo.list_recent(limit)

    # 采纳率：点赞（few-shot source=like）与点踩（bad_cases）计数
    likes = 0
    dislikes = 0
    try:
        likes = int(await runtime.db.few_shot_repo.count(source="like") or 0)
    except Exception:  # noqa: BLE001 - 反馈库不可用时采纳率记 0，不拖垮看板
        logger.warning("点赞计数失败（采纳率分子记 0）")
    try:
        dislikes = int(await runtime.db.bad_case_repo.count() or 0)
    except Exception:  # noqa: BLE001
        logger.warning("点踩计数失败（采纳率分母记 0）")

    return {
        "days": days,
        "sample": len(records),
        **aggregate_quality(records, likes=likes, dislikes=dislikes),
    }


def _as_naive(dt: datetime) -> datetime:
    """统一为朴素时间再比较（DB 存的是 MySQL NOW() 本地时间）。"""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# --------------------------------------------------------------------------
# 2.5 错误样本池（PRD FR6）：报错分类 + 限次自愈样本
# --------------------------------------------------------------------------


@router.get("/obs/error-samples", summary="错误样本池：报错分类与自愈样本（过滤 + 分页）", dependencies=_guard(PERM_OBS))
async def obs_error_samples(
    category: Optional[int] = None,
    node: Optional[str] = None,
    recovered: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    repo = _error_repo(runtime)
    if repo is None:
        return {"total": 0, "page": page, "page_size": page_size, "items": [], "stats": {}}
    items, total = await repo.query(
        category=category,
        node=node,
        recovered=recovered,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    stats = await repo.stats()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
        "stats": stats,
    }


# --------------------------------------------------------------------------
# 3. 会话维度（"每个对话显示当前对话的观测信息"）
# --------------------------------------------------------------------------


@router.get("/obs/sessions/{session_id}", summary="单个对话的观测汇总与明细", dependencies=_guard(PERM_OBS))
async def obs_session_detail(
    session_id: str, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    trace_repo, span_repo = _repos(runtime)
    records, total = await trace_repo.query(session_id=session_id, limit=500)
    if total == 0:
        raise HTTPException(status_code=404, detail="该会话暂无观测数据")
    spans = await span_repo.list_by_session(session_id, limit=2000) if span_repo else []
    return {
        "session_id": session_id,
        "summary": await trace_repo.aggregate_filtered(session_id=session_id),
        "rounds": [_summary(r) for r in records],
        "spans": aggregate_spans(spans) if spans else {"total": 0, "nodes": []},
    }


# --------------------------------------------------------------------------
# 4. 导出（保留能力，后续对接任意 OTel 后端 / 离线分析；不主动外发）
# --------------------------------------------------------------------------


def _otlp_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_otlp_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _otlp_attributes(attrs: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"key": key, "value": _otlp_value(value)}
        for key, value in (attrs or {}).items()
        if value is not None
    ]


def _to_otlp_span(s: SpanData) -> Dict[str, Any]:
    def _nano(dt: Optional[datetime], fallback: Optional[float] = None) -> str:
        if dt is not None:
            return str(int(dt.replace(tzinfo=timezone.utc).timestamp() * 1e9))
        if fallback is not None:
            return str(int(fallback * 1e9))
        return "0"

    status_code = {"OK": 1, "ERROR": 2}.get(s.status_code, 0)
    return {
        "traceId": s.trace_id,
        "spanId": s.span_id,
        "parentSpanId": s.parent_span_id or "",
        "name": s.name,
        "kind": {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}.get(
            s.kind, 1
        ),
        "startTimeUnixNano": _nano(s.start_time),
        "endTimeUnixNano": _nano(s.end_time),
        "attributes": _otlp_attributes(s.attributes),
        "events": [
            {
                "name": ev.get("name", ""),
                "timeUnixNano": _nano(ev.get("timestamp")),
                "attributes": _otlp_attributes(ev.get("attributes")),
            }
            for ev in (s.events or [])
        ],
        "status": {"code": status_code, "message": s.status_message or ""},
    }


@router.get("/obs/export", summary="导出观测数据（OTel/JSON，保留能力供后续使用）", dependencies=_guard(PERM_OBS))
async def obs_export(
    session_id: Optional[str] = None,
    round_id: Optional[str] = None,
    format: str = Query("otlp", pattern="^(otlp|json)$"),
    limit: int = Query(1000, ge=1, le=10000),
    download: bool = False,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
):
    # FR13 治理：审批闸门先于取数（越权时不该先打库），行数再按 export_max_rows 收敛
    assert_export_allowed(runtime, current, kind="obs")
    policy = export_policy(runtime)
    requested_limit = limit
    limit = min(limit, policy["max_rows"])
    trace_repo, span_repo = _repos(runtime)

    if session_id and round_id:
        records = [await trace_repo.get(session_id, round_id)]
        records = [r for r in records if r is not None]
        spans = (
            await span_repo.list_by_round(session_id, round_id) if span_repo else []
        )
    elif session_id:
        records, _ = await trace_repo.query(session_id=session_id, limit=limit)
        spans = await span_repo.list_by_session(session_id, limit=limit) if span_repo else []
    else:
        records = await trace_repo.list_recent(limit)
        spans = await span_repo.list_recent(limit) if span_repo else []

    service_name = runtime.settings.observability.service_name
    exported_at = datetime.now(timezone.utc).isoformat()

    if format == "otlp":
        # OTLP/JSON：resourceSpans -> scopeSpans -> spans（可直接被任意 OTel 后端消费）
        by_trace: Dict[str, Dict[str, Any]] = {}
        for s in spans:
            entry = by_trace.setdefault(
                s.trace_id,
                {
                    "resource": {
                        "attributes": _otlp_attributes(
                            {"service.name": s.service_name or service_name}
                        )
                    },
                    "scopeSpans": [
                        {"scope": {"name": "dmf-agent.observability"}, "spans": []}
                    ],
                },
            )
            entry["scopeSpans"][0]["spans"].append(_to_otlp_span(s))
        payload: Dict[str, Any] = {"resourceSpans": list(by_trace.values())}
    else:
        payload = {
            "service_name": service_name,
            "exported_at": exported_at,
            "otel_semconv": "https://opentelemetry.io/docs/specs/otel/trace/api/",
            "rounds": [
                {
                    **_summary(r),
                    "trace": r.trace,
                }
                for r in records
            ],
            "spans": [_span_detail(s) for s in spans],
        }

    # FR13 水印 + 审计：水印写进导出件本身（随文件流出），前端预览另叠一层屏幕水印
    exported_rows = len(records) + len(spans)
    truncated = requested_limit > policy["max_rows"] or len(records) >= limit or len(spans) >= limit
    if policy["watermark"]:
        payload["watermark"] = build_watermark(
            runtime, current, kind="obs", rows=exported_rows, truncated=truncated
        )
    await audit_export(
        runtime, current, kind="obs", rows=exported_rows, truncated=truncated
    )

    headers = {}
    if download:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "otlp.json"
        fname = (
            f"obs-{session_id[:8]}-{stamp}.{suffix}" if session_id else f"obs-{stamp}.{suffix}"
        )
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return JSONResponse(
        content=json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
        headers=headers,
    )
