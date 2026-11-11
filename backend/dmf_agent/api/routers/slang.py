"""三级黑话词典管理 API（管理面「黑话词典管理」页）。

对齐 `doc/黑话词典治理方案.md` §4。三级作用域（同名以高层级为准）：

    user（用户级，仅本人可见） > tenant（公司级，租户内共享） > industry（行业级，平台内置）

**读写路径（用户明确要求）**：
- 读：Redis 优先 → 未命中回源 MySQL → 回填 Redis（与进程内短 TTL 缓存）；
- 写：**先写 MySQL，再 `refresh_layer` 同步 Redis**（write-through，不做延迟失效）。
  这样"发布一条词条，下一句对话立刻生效"，且写失败不会污染缓存。

**权限（比 few-shot 更细，按 scope 收窄）**：
- 读 `slang`：列表 / 统计 / facets / 试改写 / 导出 / 版本；
- 写 `slang:write`：增删改 / 导入；
- 审 `slang:review`：发布 / 停用 / 回滚；
- **非 admin 只能操作 `scope=user` 且归属为本人**的词条 —— 公司级/行业级影响全租户，
  必须由 admin 维护（`require_permission` 只能判"有没有能力"，scope 归属要靠 router 守卫）。

接口：
- `GET    /slang`                  列表（scope/entry_type/status/source/keyword/layer + 分页）
- `GET    /slang/stats`            统计（按 scope/type/status 计数 + 命中 Top）
- `GET    /slang/facets`           过滤选项（含语义层标准名候选，供"标准名"下拉）
- `POST   /slang/preview`          在线试改写（复用线上 resolver，同源同口径）
- `POST   /slang`                  新增
- `POST   /slang/import`           批量导入（支持 dry_run 预检）
- `GET    /slang/export`           导出 JSON
- `GET    /slang/{id}`             详情
- `PUT    /slang/{id}`             修改（version 乐观锁）
- `DELETE /slang/{id}`             删除
- `POST   /slang/{id}/publish`     发布 / 启用
- `POST   /slang/{id}/disable`     停用
- `GET    /slang/{id}/versions`    版本历史
- `POST   /slang/{id}/rollback`    回滚（P1）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_SLANG, PERM_SLANG_REVIEW, PERM_SLANG_WRITE
from dmf_agent.recall.slang import build_slang_resolver, hash_query, validate_standard
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.slang import (
    ALL_ENTRY_TYPES,
    ALL_SCOPES,
    ALL_SOURCES,
    ALL_STATUSES,
    MODE_EXPAND,
    SCOPE_INDUSTRY,
    SCOPE_TENANT,
    SCOPE_USER,
    SOURCE_MANUAL,
    STATUS_DRAFT,
    STATUS_DISABLED,
    STATUS_PUBLISHED,
    SlangEntryData,
    SlangImportReport,
    SlangImportRequest,
    SlangPatch,
    SlangPreviewRequest,
    SlangRewriteFeedback,
    SlangUpsert,
)
from dmf_agent.security.export_gate import (
    apply_export_governance,
    assert_export_allowed,
    audit_export,
    export_policy,
)
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slang", tags=["slang"])

EXPORT_VERSION = 1


def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]


# ---------- 身份 / 作用域守卫 ----------


def _is_admin(current: CurrentUser) -> bool:
    return "*" in (current.permissions or [])


def _is_anonymous(current: CurrentUser) -> bool:
    """鉴权关闭时的合成用户（本地调试，允许显式指定归属）。"""
    return not current.id or current.id == "anonymous"


def _resolve_identity(
    current: CurrentUser, scope: str, tenant_id: str = "", user_id: str = ""
) -> tuple[str, str]:
    """按作用域解析词条的归属 (tenant_id, user_id)。

    - `industry`：无归属（"" / ""）；
    - `tenant`：`tenant_id` = 当前租户（鉴权开启时不可指定他人）；
    - `user`：`tenant_id` = 当前租户，`user_id` = 当前用户。
    鉴权关闭（合成 anonymous）时保留显式传入值，便于本地调试与测试造数。
    """
    if scope == SCOPE_INDUSTRY:
        return "", ""
    if _is_anonymous(current):
        return tenant_id or current.tenant_id or "", user_id or current.id or ""
    if scope == SCOPE_TENANT:
        return current.tenant_id or "", ""
    return current.tenant_id or "", current.id or ""


def _assert_scope_writable(
    current: CurrentUser, scope: str, tenant_id: str, user_id: str
) -> None:
    """非 admin 只能改 `scope=user` 且归属本人的词条；否则 403。"""
    if _is_admin(current) or _is_anonymous(current):
        return
    if scope != SCOPE_USER:
        raise HTTPException(
            status_code=403,
            detail="仅管理员可维护公司级 / 行业级词条（公司级 / 行业级影响全租户）",
        )
    if tenant_id != (current.tenant_id or "") or user_id != (current.id or ""):
        raise HTTPException(status_code=403, detail="只能维护本人的用户级词条")


def _assert_entry_writable(current: CurrentUser, entry: SlangEntryData) -> None:
    _assert_scope_writable(current, entry.scope, entry.tenant_id, entry.user_id)


def _resolver(runtime: AppRuntime):
    """取线上解析器（未装配时兜底现建一个，保证试改写可用）。"""
    res = getattr(runtime, "slang_resolver", None)
    if res is None:
        res = build_slang_resolver(
            runtime.db.slang_repo,
            getattr(runtime.db, "redis_client", None),
            getattr(getattr(runtime, "settings", None), "slang", None),
        )
    return res


async def _sync_cache(runtime: AppRuntime, scope: str, tenant_id: str, user_id: str) -> int:
    """写后同步 Redis（MySQL 已写成功才调用；失败只告警不阻断）。"""
    try:
        return await _resolver(runtime).refresh_layer(scope, tenant_id, user_id)
    except Exception as exc:  # noqa: BLE001 - 缓存同步失败不该让写接口 500
        logger.warning(
            "黑话词典缓存同步失败（scope=%s tenant=%s user=%s）：%s",
            scope, tenant_id, user_id, exc,
        )
        return 0


def _validate(layer: Any, entry_type: str, standard: str, dim_value: str) -> None:
    """标准名存在性校验；不过 422（附提示，避免"改写后反而解析失败"）。"""
    err = validate_standard(layer, entry_type, standard, dim_value)
    if err:
        raise HTTPException(status_code=422, detail=err)


# ---------- 列表 / 统计 / 过滤选项 ----------


@router.get("", dependencies=_guard(PERM_SLANG), summary="黑话词条列表")
async def list_entries(
    scope: Optional[str] = Query(None, pattern="^(industry|tenant|user)$"),
    entry_type: Optional[str] = Query(
        None, pattern="^(metric|dimension|dim_value|term|stop)$"
    ),
    status: Optional[str] = Query(None, pattern="^(draft|published|disabled)$"),
    source: Optional[str] = Query(None, pattern="^(builtin|manual|imported)$"),
    keyword: str = Query("", description="匹配 term / standard / remark"),
    layer: Optional[str] = Query(
        None, description="视角层级：本人可见的用户级 + 公司级 + 行业级（默认全平台）"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """列表（scope 升序 + priority 降序 + id 降序）。

    `layer=user|tenant|industry` 时收敛到当前身份可见的那一层（供前端"按层级"视图）。
    非 admin 查看全平台列表时**不隐藏**公司级/行业级（它们是共享知识），
    但用户级只回本人的（避免看到他人私有词条）。
    """
    rows, total = await runtime.db.slang_repo.query(
        scope=scope, entry_type=entry_type, status=status, source=source,
        keyword=keyword, limit=limit, offset=offset,
    )
    uid = current.id or ""
    tid = current.tenant_id or ""
    items = [
        r for r in rows
        if r.scope != SCOPE_USER
        or _is_admin(current)
        or _is_anonymous(current)
        or (r.tenant_id == tid and r.user_id == uid)
    ]
    return {
        "items": [i.model_dump() for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats", dependencies=_guard(PERM_SLANG), summary="黑话词典统计")
async def slang_stats(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """统计卡：按 scope / entry_type / status 计数 + 命中数 Top 10（失败按 0 计）。"""
    db = runtime.db

    async def _safe(coro, default=0):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - 统计失败不该把页面拖垮
            logger.warning("黑话统计失败（按 0 计）：%s", exc)
            return default

    total = await _safe(db.slang_repo.count())
    published = await _safe(db.slang_repo.count(status=STATUS_PUBLISHED))
    disabled = await _safe(db.slang_repo.count(status=STATUS_DISABLED))
    draft = await _safe(db.slang_repo.count(status=STATUS_DRAFT))
    by_scope = await _safe(db.slang_repo.count_by_scope(), {})

    # 按类型 / 来源 / 命中 Top：一次性拉取后内存聚合（词典规模有限，避免再加聚合方法）
    rows, _ = await _safe(db.slang_repo.query(limit=5000), ([], 0))
    by_type: Dict[str, int] = {t: 0 for t in ALL_ENTRY_TYPES}
    by_source: Dict[str, int] = {s: 0 for s in ALL_SOURCES}
    for r in rows:
        by_type[r.entry_type] = by_type.get(r.entry_type, 0) + 1
        by_source[r.source] = by_source.get(r.source, 0) + 1
    top_hits = sorted(
        (r for r in rows if (r.hit_count or 0) > 0),
        key=lambda r: r.hit_count or 0,
        reverse=True,
    )[:10]

    return {
        "total": total,
        "published": published,
        "draft": draft,
        "disabled": disabled,
        "by_scope": by_scope,
        "by_type": by_type,
        "by_source": by_source,
        "top_hits": [
            {
                "id": r.id,
                "term": r.term,
                "standard": r.standard,
                "entry_type": r.entry_type,
                "scope": r.scope,
                "hit_count": r.hit_count,
            }
            for r in top_hits
        ],
    }


@router.get("/metrics", dependencies=_guard(PERM_SLANG), summary="改写命中率/误改写率看板")
async def slang_metrics(
    runtime: AppRuntime = Depends(get_runtime),
    days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    """命中率/误改写率评估看板聚合：

    - 命中率 = 有命中的改写次数 / 总改写次数（近 `days` 天）；
    - 误改写率 = 反馈"误改写"数 / 已反馈总数；
    - 按类型 / 作用域的命中数分布（来自词条 hit_count 汇总）；
    - 命中 Top 词条。
    """
    db = runtime.db
    since = datetime.now() - timedelta(days=days)

    async def _safe(coro, default):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("改写指标聚合失败（降级）：%s", exc)
            return default

    events = await _safe(db.slang_repo.query_rewrite_events(since=since), [])
    total = len(events)
    with_hit = sum(1 for e in events if e.get("is_rewritten"))
    feedbacks = [e for e in events if e.get("feedback") is not None]
    good = sum(1 for e in feedbacks if e["feedback"] == 1)
    bad = sum(1 for e in feedbacks if e["feedback"] == -1)
    rated = good + bad

    # 词条命中分布（按类型 / 作用域汇总 hit_count）
    rows, _ = await _safe(db.slang_repo.query(limit=5000), ([], 0))
    by_type_hits: Dict[str, int] = {}
    by_scope_hits: Dict[str, int] = {}
    top_hits = []
    for r in rows:
        hc = r.hit_count or 0
        if hc <= 0:
            continue
        by_type_hits[r.entry_type] = by_type_hits.get(r.entry_type, 0) + hc
        by_scope_hits[r.scope] = by_scope_hits.get(r.scope, 0) + hc
        top_hits.append(
            {
                "id": r.id,
                "term": r.term,
                "standard": r.standard,
                "entry_type": r.entry_type,
                "scope": r.scope,
                "hit_count": hc,
            }
        )
    top_hits.sort(key=lambda x: x["hit_count"], reverse=True)

    return {
        "window_days": days,
        "total_attempts": total,
        "attempts_with_hit": with_hit,
        "hit_rate": round(with_hit / total, 4) if total else 0.0,
        "total_feedback": rated,
        "good": good,
        "bad": bad,
        "mis_rate": round(bad / rated, 4) if rated else 0.0,
        "by_type_hits": by_type_hits,
        "by_scope_hits": by_scope_hits,
        "top_hits": top_hits[:10],
    }


@router.post(
    "/rewrite/feedback",
    dependencies=_guard(PERM_SLANG),
    summary="改写结果反馈（误改写率数据源）",
)
async def slang_rewrite_feedback(
    body: SlangRewriteFeedback,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """前端改写徽标 👍/👎 回写：按 message_id（优先）→ query_hash 定位改写事件置 feedback；
    无对应事件则新建一条仅含反馈的记录。`cancel=True` 撤销上一次反馈（feedback 置 NULL）。"""
    event_id = await runtime.db.slang_repo.set_rewrite_feedback(
        query_hash=hash_query(body.original),
        is_correct=body.is_correct,
        note=body.note,
        session_id=body.session_id,
        message_id=body.message_id,
        cancel=body.cancel,
    )
    if body.cancel:
        return {"event_id": event_id, "is_correct": None, "cancelled": True}
    return {"event_id": event_id, "is_correct": body.is_correct}


@router.get(
    "/rewrite/feedback/status",
    dependencies=_guard(PERM_SLANG),
    summary="批量查询改写反馈状态（历史消息回显）",
)
async def slang_rewrite_feedback_status(
    message_ids: str = Query(..., description="逗号分隔的消息 id 列表"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """按 message_id 批量回显改写反馈状态（与点赞/点踩的 /feedback/status 同思路）。
    返回 {status: {message_id: 'good' | 'bad'}}，仅含已反馈（feedback 非 NULL）的消息。"""
    ids = [x for x in message_ids.split(",") if x.strip()]
    fb = await runtime.db.slang_repo.get_rewrite_feedback_by_messages(ids)
    status: Dict[str, str] = {}
    for mid, val in fb.items():
        status[mid] = "good" if val == 1 else "bad"
    return {"status": status}


@router.get("/facets", dependencies=_guard(PERM_SLANG), summary="过滤选项与标准名候选")
async def slang_facets(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """过滤下拉选项 + 语义层标准名候选（新增词条时选"标准名"）。"""
    layer = getattr(runtime, "semantic_layer", None)
    metric_names: List[str] = []
    dimension_names: List[str] = []
    if layer is not None:
        try:
            metric_names = [m.name for m in layer.list_metrics()]
            dimension_names = list(layer.dimension_names())
        except Exception as exc:  # noqa: BLE001 - 语义层不可用不阻断过滤选项
            logger.warning("加载语义层标准名失败（facets 降级）：%s", exc)

    rows, _ = await runtime.db.slang_repo.query(limit=5000)
    # 已用来源（真实出现过的）+ 标准名（真实的词汇表里出现过的）
    used_terms = sorted({r.term for r in rows if r.term})[:200]
    return {
        "scopes": list(ALL_SCOPES),
        "entry_types": list(ALL_ENTRY_TYPES),
        "statuses": list(ALL_STATUSES),
        "sources": list(ALL_SOURCES),
        "metric_names": metric_names,
        "dimension_names": dimension_names,
        "used_terms": used_terms,
    }


# ---------- 在线试改写（本页最有价值的按钮）----------


@router.post(
    "/preview", dependencies=_guard(PERM_SLANG), summary="在线试改写（复用线上解析器）"
)
async def preview_rewrite(
    body: SlangPreviewRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """输入一句话 → 返回改写句 + 命中明细（与线上改写同源同口径）。

    默认以**当前调用者身份**解析（可见自己的用户级 + 本租户公司级 + 行业级）；
    `include_draft=true` 时把草稿也纳入（发布前预演）；`extra_entries` 可内联未落库词条试效果。
    """
    tenant_id = body.tenant_id or current.tenant_id or ""
    user_id = body.user_id or current.id or ""
    if not (_is_admin(current) or _is_anonymous(current)):
        # 非 admin 只能以本人身份试改写，避免借试改写读他人词条
        tenant_id, user_id = current.tenant_id or "", current.id or ""

    extra: List[SlangEntryData] = []
    for raw in body.extra_entries or []:
        if isinstance(raw, dict):
            try:
                extra.append(SlangEntryData(**raw))
            except Exception:  # noqa: BLE001 - 单条非法不阻断
                logger.debug("忽略非法内联词条：%s", raw)

    result = await _resolver(runtime).resolve(
        body.query,
        tenant_id=tenant_id,
        user_id=user_id,
        include_draft=body.include_draft,
        extra_entries=extra or None,
    )
    return result.model_dump()


# ---------- 写入 ----------


@router.post(
    "",
    response_model=SlangEntryData,
    dependencies=_guard(PERM_SLANG_WRITE),
    summary="新增词条",
)
async def create_entry(
    body: SlangUpsert,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    """新增（校验标准名存在性；同层同类型同词重复 → 409）。"""
    term = (body.term or "").strip()
    if not term:
        raise HTTPException(status_code=422, detail="黑话词不能为空")
    tenant_id, user_id = _resolve_identity(current, body.scope, body.tenant_id, body.user_id)
    _assert_scope_writable(current, body.scope, tenant_id, user_id)

    entry_type = body.entry_type or "term"
    if entry_type not in ALL_ENTRY_TYPES:
        raise HTTPException(status_code=422, detail=f"未知词条类型：{entry_type}")
    standard = (body.standard or "").strip()
    dim_value = (body.dim_value or "").strip()
    _validate(getattr(runtime, "semantic_layer", None), entry_type, standard, dim_value)

    candidate = SlangEntryData(
        scope=body.scope,
        tenant_id=tenant_id,
        user_id=user_id,
        term=term,
        standard=standard,
        entry_type=entry_type,
        dim_value=dim_value,
        rewrite_mode=body.rewrite_mode or MODE_EXPAND,
        priority=body.priority,
        source=SOURCE_MANUAL,
        status=body.status if body.status in ALL_STATUSES else STATUS_DRAFT,
        confidence=1.0,
        remark=body.remark or "",
        created_by=current.username or current.id or "system",
    )
    existing = await runtime.db.slang_repo.find(candidate)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"同层级已存在相同黑话「{term}」（id={existing.id}，类型={existing.entry_type}）",
        )
    created = await runtime.db.slang_repo.add(candidate)
    await _sync_cache(runtime, created.scope, created.tenant_id, created.user_id)
    logger.info(
        "新增黑话词条：id=%s term=%s scope=%s type=%s",
        created.id, created.term, created.scope, created.entry_type,
    )
    return created


@router.post(
    "/import",
    response_model=SlangImportReport,
    dependencies=_guard(PERM_SLANG_WRITE),
    summary="批量导入（支持 dry_run 预检）",
)
async def import_entries(
    body: SlangImportRequest,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangImportReport:
    """导入 JSON 词条数组。`dry_run=true` 只做校验与冲突检测、不落库。

    每层导入完成后按 (scope, tenant_id, user_id) 去重刷新 Redis。
    """
    report = SlangImportReport(total=len(body.entries or []))
    tenant_id, user_id = _resolve_identity(
        current, body.scope, body.tenant_id, body.user_id
    )
    _assert_scope_writable(current, body.scope, tenant_id, user_id)

    repo = runtime.db.slang_repo
    layer = getattr(runtime, "semantic_layer", None)
    touched: set = set()
    for idx, raw in enumerate(body.entries or []):
        if not isinstance(raw, dict):
            report.errors.append({"index": idx + 1, "error": "不是对象"})
            report.invalid += 1
            continue
        term = str(raw.get("term") or "").strip()
        if not term:
            report.errors.append({"index": idx + 1, "error": "缺少 term"})
            report.invalid += 1
            continue
        entry_type = str(raw.get("entry_type") or "term")
        if entry_type not in ALL_ENTRY_TYPES:
            report.errors.append(
                {"index": idx + 1, "term": term, "error": f"未知类型 {entry_type}"}
            )
            report.invalid += 1
            continue
        standard = str(raw.get("standard") or "").strip()
        dim_value = str(raw.get("dim_value") or "").strip()
        err = validate_standard(layer, entry_type, standard, dim_value)
        if err:
            report.errors.append({"index": idx + 1, "term": term, "error": err})
            report.invalid += 1
            continue
        status = str(raw.get("status") or body.status or STATUS_DRAFT)
        if status not in ALL_STATUSES:
            status = STATUS_DRAFT
        candidate = SlangEntryData(
            scope=body.scope,
            tenant_id=tenant_id,
            user_id=user_id,
            term=term,
            standard=standard,
            entry_type=entry_type,
            dim_value=dim_value,
            rewrite_mode=str(raw.get("rewrite_mode") or body.rewrite_mode or MODE_EXPAND),
            priority=int(raw.get("priority") or 100),
            source=SOURCE_MANUAL if body.source == SOURCE_MANUAL else body.source,
            status=status,
            confidence=float(raw.get("confidence") or 1.0),
            remark=str(raw.get("remark") or ""),
            created_by=current.username or current.id or "system",
        )
        existing = await repo.find(candidate)
        if existing is not None:
            if not body.overwrite:
                report.skipped += 1
                report.conflicts.append(
                    {"index": idx + 1, "term": term, "existing_id": existing.id}
                )
                continue
            if body.dry_run:
                report.updated += 1
                continue
            updated = await repo.update(
                existing.id,
                term=candidate.term,
                standard=candidate.standard,
                entry_type=candidate.entry_type,
                dim_value=candidate.dim_value,
                rewrite_mode=candidate.rewrite_mode,
                priority=candidate.priority,
                status=candidate.status,
                remark=candidate.remark,
                operator=current.username or current.id or "system",
            )
            if updated is not None:
                report.updated += 1
                touched.add((updated.scope, updated.tenant_id, updated.user_id))
            continue
        if body.dry_run:
            report.created += 1
            continue
        created = await repo.add(candidate)
        report.created += 1
        touched.add((created.scope, created.tenant_id, created.user_id))

    if not body.dry_run:
        for scope, tid, uid in touched:
            await _sync_cache(runtime, scope, tid, uid)
    logger.info(
        "导入黑话词条（dry_run=%s）：created=%s updated=%s skipped=%s invalid=%s",
        body.dry_run, report.created, report.updated, report.skipped, report.invalid,
    )
    return report


@router.get("/export", dependencies=_guard(PERM_SLANG), summary="导出词条（JSON）")
async def export_entries(
    scope: Optional[str] = Query(None, pattern="^(industry|tenant|user)$"),
    entry_type: Optional[str] = Query(
        None, pattern="^(metric|dimension|dim_value|term|stop)$"
    ),
    status: Optional[str] = Query(None, pattern="^(draft|published|disabled)$"),
    source: Optional[str] = Query(None, pattern="^(builtin|manual|imported)$"),
    keyword: str = Query(""),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """导出为 JSON（前端用 Blob 落盘，避免新窗口丢 Authorization 头）。

    FR13 治理：先过审批闸门（越权时不查库）→ 按 `security.export_max_rows` 取数并截断 →
    返回体带 `watermark`（导出人/租户/时间/行数）→ 写导出审计。
    """
    assert_export_allowed(runtime, current, kind="slang")
    # 多取一行做「溢出探测」：若把 limit 直接设成 max_rows，截断会变成静默的——
    # 下游 apply_export_governance 只看到「刚好 cap 行」，无从判断是否还有更多。
    cap = export_policy(runtime)["max_rows"]
    rows, _ = await runtime.db.slang_repo.query(
        scope=scope, entry_type=entry_type, status=status, source=source,
        keyword=keyword, limit=cap + 1, offset=0,
    )
    raw_overflow = len(rows) > cap
    uid = current.id or ""
    tid = current.tenant_id or ""
    visible = [
        r for r in rows
        if r.scope != SCOPE_USER
        or _is_admin(current)
        or _is_anonymous(current)
        or (r.tenant_id == tid and r.user_id == uid)
    ]
    visible, watermark = apply_export_governance(
        runtime, current, kind="slang", rows=visible
    )
    # 取数已触顶即视为截断（即便用户级过滤后可见行数恰好 < cap，也已无法保证拿到全量）
    truncated = raw_overflow or bool(watermark and watermark.get("truncated"))
    if watermark is not None:
        watermark["truncated"] = truncated
    await audit_export(runtime, current, kind="slang", rows=len(visible), truncated=truncated)
    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(visible),
        "watermark": watermark,
        "items": [
            {
                "term": r.term,
                "standard": r.standard,
                "entry_type": r.entry_type,
                "dim_value": r.dim_value,
                "rewrite_mode": r.rewrite_mode,
                "priority": r.priority,
                "scope": r.scope,
                "source": r.source,
                "status": r.status,
                "remark": r.remark,
            }
            for r in visible
        ],
    }


# ---------- 单条：detail / update / delete / publish / disable / versions / rollback ----------
# 注意：静态路径（/stats /facets /preview /import /export）必须声明在 /{entry_id} 之前。


async def _get_or_404(runtime: AppRuntime, entry_id: int) -> SlangEntryData:
    entry = await runtime.db.slang_repo.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    return entry


@router.get(
    "/{entry_id}", response_model=SlangEntryData, dependencies=_guard(PERM_SLANG),
    summary="词条详情",
)
async def get_entry(
    entry_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    entry = await _get_or_404(runtime, entry_id)
    if (
        entry.scope == SCOPE_USER
        and not (_is_admin(current) or _is_anonymous(current))
        and (entry.tenant_id != (current.tenant_id or "") or entry.user_id != (current.id or ""))
    ):
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    return entry


@router.put(
    "/{entry_id}", response_model=SlangEntryData, dependencies=_guard(PERM_SLANG_WRITE),
    summary="修改词条（version 乐观锁）",
)
async def update_entry(
    entry_id: int,
    body: SlangPatch,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    entry = await _get_or_404(runtime, entry_id)
    _assert_entry_writable(current, entry)
    if body.version is not None and body.version != entry.version:
        raise HTTPException(
            status_code=409,
            detail=f"版本冲突：词条已被修改（当前 version={entry.version}，提交 version={body.version}）",
        )

    fields = body.model_dump(exclude_none=True, exclude={"version"})
    entry_type = fields.get("entry_type", entry.entry_type)
    standard = fields.get("standard", entry.standard)
    dim_value = fields.get("dim_value", entry.dim_value)
    if entry_type in ("metric", "dimension", "dim_value"):
        _validate(getattr(runtime, "semantic_layer", None), entry_type, standard or "", dim_value or "")

    updated = await runtime.db.slang_repo.update(
        entry_id,
        **fields,
        operator=current.username or current.id or "system",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    await _sync_cache(runtime, updated.scope, updated.tenant_id, updated.user_id)
    logger.info("更新黑话词条：id=%s version=%s", entry_id, updated.version)
    return updated


@router.delete(
    "/{entry_id}", dependencies=_guard(PERM_SLANG_WRITE), summary="删除词条"
)
async def delete_entry(
    entry_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    entry = await _get_or_404(runtime, entry_id)
    _assert_entry_writable(current, entry)
    ok = await runtime.db.slang_repo.delete(
        entry_id, operator=current.username or current.id or "system"
    )
    if not ok:
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    await _sync_cache(runtime, entry.scope, entry.tenant_id, entry.user_id)
    logger.info("删除黑话词条：id=%s term=%s", entry_id, entry.term)
    return {"deleted": True, "id": entry_id}


@router.post(
    "/{entry_id}/publish", response_model=SlangEntryData,
    dependencies=_guard(PERM_SLANG_REVIEW), summary="发布 / 启用",
)
async def publish_entry(
    entry_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    return await _set_status(entry_id, STATUS_PUBLISHED, current, runtime)


@router.post(
    "/{entry_id}/disable", response_model=SlangEntryData,
    dependencies=_guard(PERM_SLANG_REVIEW), summary="停用",
)
async def disable_entry(
    entry_id: int,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    return await _set_status(entry_id, STATUS_DISABLED, current, runtime)


async def _set_status(
    entry_id: int, status: str, current: CurrentUser, runtime: AppRuntime
) -> SlangEntryData:
    entry = await _get_or_404(runtime, entry_id)
    _assert_entry_writable(current, entry)
    updated = await runtime.db.slang_repo.update(
        entry_id, status=status, operator=current.username or current.id or "system"
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    await _sync_cache(runtime, updated.scope, updated.tenant_id, updated.user_id)
    logger.info("黑话词条状态变更：id=%s status=%s", entry_id, status)
    return updated


@router.get(
    "/{entry_id}/versions", dependencies=_guard(PERM_SLANG), summary="版本历史"
)
async def list_entry_versions(
    entry_id: int,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    await _get_or_404(runtime, entry_id)
    versions = await runtime.db.slang_repo.list_versions(entry_id)
    return {
        "entry_id": entry_id,
        "count": len(versions),
        "items": [v.model_dump(mode="json") for v in versions],
    }


@router.post(
    "/{entry_id}/rollback", response_model=SlangEntryData,
    dependencies=_guard(PERM_SLANG_REVIEW), summary="回滚到指定版本（P1）",
)
async def rollback_entry(
    entry_id: int,
    body: Dict[str, Any],
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> SlangEntryData:
    """回滚：把目标版本的 `after_json` 重新写回（生成一条新版本，不删历史）。

    body: `{"version": <int>, "remark": "..."}`
    """
    target_version = body.get("version")
    if not isinstance(target_version, int):
        raise HTTPException(status_code=422, detail="缺少整数字段 version")
    entry = await _get_or_404(runtime, entry_id)
    _assert_entry_writable(current, entry)
    versions = await runtime.db.slang_repo.list_versions(entry_id)
    snapshot = next((v for v in versions if v.version == target_version), None)
    if snapshot is None or not snapshot.after_json:
        raise HTTPException(status_code=404, detail=f"版本 {target_version} 不存在或不可回滚")
    snap = snapshot.after_json
    updated = await runtime.db.slang_repo.update(
        entry_id,
        term=str(snap.get("term") or entry.term),
        standard=str(snap.get("standard") or ""),
        entry_type=str(snap.get("entry_type") or entry.entry_type),
        dim_value=str(snap.get("dim_value") or ""),
        rewrite_mode=str(snap.get("rewrite_mode") or entry.rewrite_mode),
        priority=int(snap.get("priority") or entry.priority),
        status=str(snap.get("status") or entry.status),
        remark=str(body.get("remark") or f"回滚至 v{target_version}"),
        operator=current.username or current.id or "system",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="黑话词条不存在")
    await _sync_cache(runtime, updated.scope, updated.tenant_id, updated.user_id)
    logger.info("黑话词条回滚：id=%s -> v%s", entry_id, target_version)
    return updated
