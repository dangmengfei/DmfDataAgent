"""语义注册中心 API（P1 只读骨架 + P2 写路径：版本 / 审批 / 回滚）。

对齐 `doc/语义层注册中心与指标权限治理方案.md` §3.8：
- 读：统计卡 / 过滤选项 / 模型·维度·度量·指标的列表与详情；
- 写（P2）：新建草稿 / 编辑 / 校验 / 提交审核 / 审批发布 / 废弃 / 版本历史 / 回滚。

权限分层（口径审批 = 起草与审批分离）：
- 读 `semantic`（与语义查询页一致；admin 通配）；
- 写 `semantic:write`：new draft / 编辑 / 提交审核 / 废弃 / 校验（analyst + admin）；
- 审批 `admin`（角色守卫）：审批发布 / 回滚（仅管理员，确保审批权不落在起草人）。

**静态路径（stats/facets/draft）必须注册在 `/{key}` 之前**（fewshots/slang 踩过的坑）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import require_permission, require_role
from dmf_agent.auth.roles import ADMIN, PERM_SEMANTIC, PERM_SEMANTIC_WRITE
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.semantic.governance import (
    validate_dimension_definition,
    validate_metric_definition,
)
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.overlay import (
    SNAPSHOT_FULL,
    SNAPSHOT_OVERLAY,
    normalize_mode,
    summarize_overlay,
)
from dmf_agent.semantic.registry import SemanticRegistry
from dmf_agent.services.runtime import AppRuntime
from dmf_agent.services.semantic_layer import refresh_semantic_layer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/semantic/registry", tags=["semantic-registry"])


def _guard(permission: str) -> list:
    return [Depends(require_permission(permission))]


def _repo(runtime: AppRuntime):
    db = runtime.db
    repo = getattr(db, "semantic_registry_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="语义注册中心仓储未装配")
    return repo


# ---------- 租户作用域（**读得到 ≠ 改得动**，两套语义刻意分开）----------
#
# 注册中心治理 CRUD 的租户隔离口径：
#   - 读（可见性）：非 admin 恒为本租户（全局口径照常可见、**只读**）；
#     admin 不传 ``tenant_id`` = 全量视角（跨租户审批需要看得见租户草稿），
#     传了则按该值收敛（"" 仅全局、具体 id 为全局 + 该租户）。
#   - 写（归属）：非 admin 恒为本租户，显式传别的值（含 "" 全局）一律 403 ——
#     "租户改到全局定义"在入口就被拒，而不是指望每个调用点都记得校验；
#     admin 默认写全局（既有行为：全局口径由管理员维护），可显式指定目标租户。


def _is_admin(current: Optional[CurrentUser]) -> bool:
    if current is None:
        return True  # 未走鉴权的内部直调：按系统视角处理（测试 / 启动引导）
    role = (getattr(current, "role", "") or "").strip().lower()
    perms = set(getattr(current, "permissions", None) or [])
    return role == ADMIN or "*" in perms


def _read_scope(current: Optional[CurrentUser], tenant_id: Optional[str]) -> Optional[str]:
    """读取作用域（可见性）；返回 None = 不过滤（全量系统视角）。"""
    if not _is_admin(current):
        return (getattr(current, "tenant_id", "") or "").strip()
    if tenant_id is None:
        return None
    return (tenant_id or "").strip()


def _write_tenant(
    current: Optional[CurrentUser], tenant_id: Optional[str] = None
) -> str:
    """写入归属（归属，不是可见性）；非 admin 不可越写。"""
    own = (getattr(current, "tenant_id", "") or "").strip()
    if not _is_admin(current):
        if tenant_id is not None and (tenant_id or "").strip() != own:
            raise HTTPException(status_code=403, detail="非管理员只能在本租户下写入语义定义")
        return own
    if tenant_id is None:
        return ""
    return (tenant_id or "").strip()


# ---------- 统计 / facets（静态路径，先注册） ----------


@router.get("/stats", dependencies=_guard(PERM_SEMANTIC), summary="注册中心统计卡")
async def registry_stats(
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    return await _repo(runtime).stats(tenant_id=_read_scope(current, tenant_id))


@router.get("/facets", dependencies=_guard(PERM_SEMANTIC), summary="过滤选项")
async def registry_facets(
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    return await _repo(runtime).facets(tenant_id=_read_scope(current, tenant_id))


# ---------- 语义层发布快照（混合方案：YAML 基座 → DB 发布 → 运行时装配） ----------
#
# 快照审批流与指标级状态机同构：**草稿不能直接发布**，必须经 review 闸口。
# 运行时只消费 state=published；审批通过后自动热更新（无需重启）。


class PublishSnapshotRequest(BaseModel):
    """发布请求体。

    - ``tenant_id`` 空 = 全局默认快照；
    - ``mode``：``full``（整份已合并 manifest，源自当前 YAML 基座）或
      ``overlay``（只含差异条目，必须随 ``payload`` 提供，用于局部租户覆盖）；
    - ``state`` 默认 ``draft`` —— 走审批闸口（draft → review → published）。
      admin 确有需要时可显式传 ``published`` 一步到位。
    """

    tenant_id: str = ""
    mode: str = SNAPSHOT_FULL
    state: str = "draft"
    payload: Optional[Dict[str, Any]] = None


class SnapshotRollbackRequest(BaseModel):
    """快照回滚请求体：回滚后生成的新版本状态（默认直接 published）。"""

    state: str = "published"


class SnapshotRejectRequest(BaseModel):
    """快照驳回请求体：必须填写原因。"""

    reason: str = Field(..., min_length=1, description="驳回原因（必填）")


class SnapshotBatchRequest(BaseModel):
    """批量审批/驳回请求体。"""

    action: str = Field(..., description="approve | reject")
    ids: List[int] = Field(..., min_length=1, description="快照 id 列表")
    reason: Optional[str] = Field(None, description="reject 时必填")


def _publish_repo(runtime: AppRuntime):
    repo = getattr(runtime.db, "semantic_published_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="语义层发布仓储未装配")
    return repo


def _yaml_layer(runtime: AppRuntime) -> SemanticLayer:
    """当前 YAML 基座（manifest.d 目录或单文件）；发布动作以此为源。"""
    cfg = getattr(runtime.settings, "semantic", None)
    path = ((getattr(cfg, "manifest_dir", "") or "").strip()) or None
    return SemanticLayer(path)


def _registry(runtime: AppRuntime) -> SemanticRegistry:
    return SemanticRegistry(_publish_repo(runtime))


def _snapshot_view(rec) -> Dict[str, Any]:
    """快照的管理面视图：计数 + overlay 摘要（不回传完整 payload，避免响应体过重）。"""
    payload = rec.payload or {}
    view: Dict[str, Any] = {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "version": rec.version,
        "state": rec.state,
        "mode": rec.mode or SNAPSHOT_FULL,
        "created_by": rec.created_by,
        "created_at": rec.created_at,
        "published_at": rec.published_at,
        "model_count": len(payload.get("semantic_models") or []),
        "metric_count": len(payload.get("metrics") or []),
        "approved_by": getattr(rec, "approved_by", "") or "",
        "rejected_by": getattr(rec, "rejected_by", "") or "",
        "reject_reason": getattr(rec, "reject_reason", "") or "",
    }
    if view["mode"] == SNAPSHOT_OVERLAY:
        view["overlay"] = summarize_overlay(payload)
    return view


def _invalidate_scope(runtime: AppRuntime, tenant_id: str = "") -> None:
    """快照变更后清掉本进程的租户语义层缓存。

    链指纹比对本身已能自愈（下一请求发现链变了会自动重建），这里只是省掉一次
    无用查询；``tenant_id=""`` 表示全局变更 → 清空全部（所有租户链都依赖全局链）。
    失效失败只告警：变更已落库，不会因为缓存没清而丢数据。
    """
    service = getattr(runtime, "semantic_scope", None)
    if service is None:
        return
    try:
        service.invalidate(tenant_id or None)
    except Exception:  # noqa: BLE001 - 缓存失效失败不影响变更结果
        logger.warning("语义层租户缓存失效失败（将由链指纹自愈）", exc_info=True)


async def _safe_refresh(
    runtime: AppRuntime, tenant_id: str = ""
) -> Optional[Dict[str, Any]]:
    """审批/发布后尝试热更新；失败只告警（变更已落库，重启即可加载）。

    先失效租户缓存再热更新：热更新本身会 rebase 并清缓存，但**热更新失败时**
    也必须保证后续请求不会继续命中旧链的缓存。
    """
    _invalidate_scope(runtime, tenant_id)
    try:
        resolved = await refresh_semantic_layer(runtime)
    except Exception as exc:  # noqa: BLE001 - 热更新失败不影响审批结果
        logger.warning("语义层热更新失败（变更已落库，重启后可加载）：%s", exc)
        return None
    return resolved.to_dict()


@router.post(
    "/publish",
    dependencies=[Depends(require_role(ADMIN))],
    summary="把当前 YAML 语义层发布为快照（默认落 draft，走审批闸口；仅 admin）",
)
async def registry_publish_snapshot(
    body: PublishSnapshotRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        mode = normalize_mode(body.mode)  # 非法 mode -> 400
        layer = None
        if mode == SNAPSHOT_FULL and body.payload is None:
            # 整份定义取自当前 YAML 基座。overlay 不需要 layer：
            # 差异条目只能由 payload 提供（拿整份定义当 overlay 必然错）
            layer = _yaml_layer(runtime)
        rec = await _registry(runtime).publish(
            layer=layer,
            tenant_id=body.tenant_id,
            by=_operator(current),
            state=body.state,
            mode=mode,
            payload=body.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 新快照可能立刻改变租户链（若直接落 published）；draft 也让链指纹变化，
    # 一并失效以避免缓存里留着"没有这条记录"的旧指纹。
    _invalidate_scope(runtime, rec.tenant_id)
    return {"snapshot": _snapshot_view(rec)}


@router.get(
    "/published",
    dependencies=_guard(PERM_SEMANTIC),
    summary="列出语义层发布快照版本（租户可见性隔离）",
)
async def registry_list_published(
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域：不传=admin 全量，空串=仅全局，具体 id=全局+该租户；非 admin 固定为本租户+全局(只读)"),
    limit: int = Query(50, ge=1, le=200),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    scope = _read_scope(current, tenant_id)
    rows = await _registry(runtime).list_versions(tenant_id=scope, limit=limit, visible_scope=True)
    items = [_snapshot_view(r) for r in rows]
    return {"items": items, "total": len(items)}


# ---------- 快照审批流（draft → review → published → deprecated） ----------


@router.get(
    "/snapshots",
    dependencies=_guard(PERM_SEMANTIC),
    summary="快照列表（可按 state/租户过滤，附 overlay 摘要，租户可见性隔离）",
)
async def registry_list_snapshots(
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域：不传=admin 全量，空串=仅全局，具体 id=全局+该租户；非 admin 固定为本租户+全局(只读)"),
    state: Optional[str] = Query(None, description="draft|review|published|deprecated"),
    limit: int = Query(50, ge=1, le=200),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    scope = _read_scope(current, tenant_id)
    rows = await _registry(runtime).list_versions(tenant_id=scope, limit=limit, visible_scope=True)
    if state:
        rows = [r for r in rows if r.state == state]
    items = [_snapshot_view(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get(
    "/snapshots/{snapshot_id}",
    dependencies=_guard(PERM_SEMANTIC),
    summary="快照详情（include_payload=true 时回传完整定义）",
)
async def registry_snapshot_detail(
    snapshot_id: int,
    include_payload: bool = Query(False),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    rec = await _registry(runtime).get_snapshot(snapshot_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"快照不存在：{snapshot_id}")
    view = _snapshot_view(rec)
    if include_payload:
        view["payload"] = rec.payload
    return view


def _transition_or_http(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/snapshots/{snapshot_id}/submit",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="提交快照审核（draft → review）",
)
async def registry_submit_snapshot(
    snapshot_id: int,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).submit(snapshot_id)
    except (LookupError, ValueError) as exc:
        raise _transition_or_http(exc)
    _invalidate_scope(runtime, rec.tenant_id)
    return {"snapshot": _snapshot_view(rec)}


@router.post(
    "/snapshots/{snapshot_id}/approve",
    dependencies=[Depends(require_role(ADMIN))],
    summary="审批发布快照（review → published，仅 admin；通过后热更新）",
)
async def registry_approve_snapshot(
    snapshot_id: int,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).approve(snapshot_id, by=_operator(current))
    except (LookupError, ValueError) as exc:
        raise _transition_or_http(exc)
    return {
        "snapshot": _snapshot_view(rec),
        "runtime": await _safe_refresh(runtime, rec.tenant_id),
    }


@router.post(
    "/snapshots/{snapshot_id}/deprecate",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="废弃快照（published → deprecated；运行时退回上一个 published）",
)
async def registry_deprecate_snapshot(
    snapshot_id: int,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).deprecate(snapshot_id)
    except (LookupError, ValueError) as exc:
        raise _transition_or_http(exc)
    return {
        "snapshot": _snapshot_view(rec),
        "runtime": await _safe_refresh(runtime, rec.tenant_id),
    }


@router.post(
    "/snapshots/{snapshot_id}/revive",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="复活为草稿（deprecated → draft）",
)
async def registry_revive_snapshot(
    snapshot_id: int,
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).revive(snapshot_id)
    except (LookupError, ValueError) as exc:
        raise _transition_or_http(exc)
    _invalidate_scope(runtime, rec.tenant_id)
    return {"snapshot": _snapshot_view(rec)}


@router.post(
    "/snapshots/{snapshot_id}/rollback",
    dependencies=[Depends(require_role(ADMIN))],
    summary="回滚到该快照（生成新版本而非物理回退；仅 admin；成功后热更新）",
)
async def registry_rollback_snapshot(
    snapshot_id: int,
    body: SnapshotRollbackRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).rollback(
            snapshot_id=snapshot_id, by=_operator(current), state=body.state
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "snapshot": _snapshot_view(rec),
        "runtime": await _safe_refresh(runtime, rec.tenant_id),
    }


@router.post(
    "/snapshots/{snapshot_id}/reject",
    dependencies=[Depends(require_role(ADMIN))],
    summary="驳回快照（review → draft，记录驳回人/原因；仅 admin）",
)
async def registry_reject_snapshot(
    snapshot_id: int,
    body: SnapshotRejectRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        rec = await _registry(runtime).reject(snapshot_id, by=_operator(current), reason=body.reason)
    except (LookupError, ValueError) as exc:
        raise _transition_or_http(exc)
    _invalidate_scope(runtime, rec.tenant_id)
    return {"snapshot": _snapshot_view(rec)}


@router.post(
    "/snapshots/batch",
    dependencies=[Depends(require_role(ADMIN))],
    summary="批量审批/驳回（仅 admin；逐条执行并容错收集失败项）",
)
async def registry_batch_snapshots(
    body: SnapshotBatchRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    action = (body.action or "").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action 仅支持 approve / reject")
    if not body.ids:
        raise HTTPException(status_code=422, detail="ids 不能为空")
    registry = _registry(runtime)
    if action == "approve":
        result = await registry.batch_approve(body.ids, by=_operator(current))
    else:
        try:
            result = await registry.batch_reject(body.ids, by=_operator(current), reason=body.reason)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    # 任一状态变化都清缓存；批量里可能跨多个租户，直接清全部最稳妥
    _invalidate_scope(runtime, "")
    return result



# ---------- 运行时来源 / 版本可见性 + 热更新 ----------


async def _state_counts(registry: SemanticRegistry, tenant_id: str) -> Dict[str, int]:
    rows = await registry.list_versions(tenant_id=tenant_id, limit=200)
    counts: Dict[str, int] = {}
    for rec in rows:
        counts[rec.state] = counts.get(rec.state, 0) + 1
    return counts


@router.get(
    "/base-manifest",
    dependencies=_guard(PERM_SEMANTIC),
    summary="当前生效基座的完整清单（overlay 编辑面的条目源）",
)
async def registry_base_manifest(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """返回**已合并**的原始 manifest 字典（``semantic_models`` / ``metrics`` 全字段）。

    overlay 是**按 name 整条替换**（不做字段级 merge，见 ``semantic/overlay.py`` 的设计说明），
    所以编辑面必须以"基座里的完整条目"为起点提交，而不是只发改动的字段 ——
    这个接口就是给编辑面取这份起点的。
    """
    layer = getattr(runtime, "semantic_layer", None)
    manifest = getattr(layer, "manifest", None)
    if manifest is None:
        raise HTTPException(status_code=503, detail="运行时语义层未装配")
    return manifest.to_dict()


@router.get(
    "/runtime",
    dependencies=_guard(PERM_SEMANTIC),
    summary="当前运行时语义层来源 / 生效版本链 / 是否有更新未加载",
)
async def registry_runtime_layer(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    # 先拉一次全局链：本接口只读 runtime.semantic_resolution，若不探测就会显示"早已作废的
    # 基座"，面板上的 stale 也永远亮着。探测失败只告警（只读面板不因此整页失败）。
    service = getattr(runtime, "semantic_scope", None)
    if service is not None:
        try:
            await service.probe_base()
        except Exception:  # noqa: BLE001
            logger.warning("语义层基座探测失败（面板按现有基座展示）", exc_info=True)

    resolved = getattr(runtime, "semantic_resolution", None)
    loaded = resolved.to_dict() if resolved is not None else None
    body: Dict[str, Any] = {"loaded": loaded}

    try:
        yaml_layer = _yaml_layer(runtime)
        body["yaml_base"] = {
            "path": str(yaml_layer.manifest.path),
            "models": len(yaml_layer.manifest.models),
            "metrics": len(yaml_layer.list_metrics()),
        }
    except Exception as exc:  # noqa: BLE001 - 只读面板，不因基座异常整页失败
        body["yaml_base"] = {"error": str(exc)}

    registry = _registry(runtime)
    tenant_id = getattr(getattr(runtime, "semantic_resolution", None), "tenant_id", "") or ""
    latest: Dict[str, Any] = {}
    for label, tid in (("global", ""), ("tenant", tenant_id)):
        if label == "tenant" and not tid:
            continue
        try:
            rec = await registry.latest_published(tenant_id=tid)
            latest[label] = _snapshot_view(rec) if rec is not None else None
        except Exception as exc:  # noqa: BLE001
            latest[label] = {"error": str(exc)}
        try:
            latest[f"{label}_state_counts"] = await _state_counts(registry, tid)
        except Exception as exc:  # noqa: BLE001
            latest[f"{label}_state_counts"] = {"error": str(exc)}
    body["latest"] = latest

    # 运行中进程是否落后于 DB：最新 published 不在当前生效链上 → 需要热更新
    loaded_ids = {c.get("id") for c in ((loaded or {}).get("chain") or [])}
    newest = latest.get("tenant") if isinstance(latest.get("tenant"), dict) and latest.get("tenant") else latest.get("global")
    stale = bool(isinstance(newest, dict) and newest.get("id") not in loaded_ids)
    body["stale"] = stale

    # 请求级租户装配概况（缓存命中 / 重建次数 / 各租户生效链）：
    # 多租户下"某个租户用的是哪份定义"不该只靠推演，面板要能直接看见。
    if service is not None:
        try:
            body["scope_cache"] = service.stats()
        except Exception as exc:  # noqa: BLE001 - 只读面板不因统计失败整页失败
            body["scope_cache"] = {"error": str(exc)}
    return body


@router.post(
    "/reload",
    dependencies=[Depends(require_role(ADMIN))],
    summary="热更新运行时语义层（不重启进程；仅 admin）",
)
async def registry_reload_runtime(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    resolved = await refresh_semantic_layer(runtime)
    return {"runtime": resolved.to_dict()}


# ---------- 语义模型 ----------


@router.get("/models", dependencies=_guard(PERM_SEMANTIC), summary="语义模型列表")
async def registry_models(
    status: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None, description="命中 model_key/name/description"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items, total = await _repo(runtime).list_models(
        status=status,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
        tenant_id=_read_scope(current, tenant_id),
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/models/{model_key}", dependencies=_guard(PERM_SEMANTIC), summary="语义模型详情")
async def registry_model_detail(
    model_key: str,
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    payload = await _repo(runtime).get_model(
        model_key, tenant_id=_read_scope(current, None)
    )
    if payload is None:
        raise HTTPException(status_code=404, detail=f"语义模型不存在：{model_key}")
    return payload


# ---------- 维度 / 度量 ----------


@router.get("/dimensions", dependencies=_guard(PERM_SEMANTIC), summary="维度列表")
async def registry_dimensions(
    model_key: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items, total = await _repo(runtime).list_dimensions(
        model_key=model_key,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
        tenant_id=_read_scope(current, tenant_id),
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/measures", dependencies=_guard(PERM_SEMANTIC), summary="度量列表")
async def registry_measures(
    model_key: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items, total = await _repo(runtime).list_measures(
        model_key=model_key,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
        tenant_id=_read_scope(current, tenant_id),
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------- 指标 ----------


@router.get("/metrics", dependencies=_guard(PERM_SEMANTIC), summary="指标列表")
async def registry_metrics(
    status: Optional[str] = Query(None),
    metric_type: Optional[str] = Query(None, description="simple|ratio|derived"),
    keyword: Optional[str] = Query(None, description="命中 name/metric_key/aliases"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    tenant_id: Optional[str] = Query(None, description="租户作用域（仅 admin 可指定；空=全局）"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items, total = await _repo(runtime).list_metrics(
        status=status,
        metric_type=metric_type,
        keyword=keyword,
        limit=page_size,
        offset=(page - 1) * page_size,
        tenant_id=_read_scope(current, tenant_id),
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get(
    "/metrics/{metric_key}",
    dependencies=_guard(PERM_SEMANTIC),
    summary="指标详情（含宿主模型；血缘 P2）",
)
async def registry_metric_detail(
    metric_key: str,
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    payload = await _repo(runtime).get_metric(
        metric_key, tenant_id=_read_scope(current, None)
    )
    if payload is None:
        raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
    return payload


# ============================================================================
# 写路径：指标（P2：版本 / 审批 / 回滚）
#   起草/编辑/提交/废弃/校验：semantic:write（analyst + admin）
#   审批发布/回滚：admin 角色（审批权与起草权分离）
# ============================================================================


class RollbackRequest(BaseModel):
    """回滚请求体：目标版本号。"""

    version: int


def _operator(current: CurrentUser) -> str:
    return current.username or current.id or "system"


@router.post(
    "/metrics/draft",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="新建指标草稿",
)
async def registry_create_metric_draft(
    body: Dict[str, Any],
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    # 归属由**当前用户**决定。请求体是自由字典（LLM/前端都可能原样回显 tenant_id），
    # 拿它判断"是否越权"太脆：非 admin 一律忽略并静默收敛到本租户（写不到全局即可）；
    # admin 才采纳显式目标（不给则默认全局）。
    payload = dict(body)
    declared = payload.pop("tenant_id", None)
    tid = _write_tenant(current, declared if _is_admin(current) else None)
    item = await _repo(runtime).create_metric_draft(
        payload, operator=_operator(current), tenant_id=tid
    )
    return {"metric": item}


@router.put(
    "/metrics/{metric_key}",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="编辑指标（草稿/待审）",
)
async def registry_update_metric(
    metric_key: str,
    body: Dict[str, Any],
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).update_metric(
            metric_key, body, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
    return {"metric": item}


@router.post(
    "/metrics/{metric_key}/validate",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="校验指标口径（六道闸）",
)
async def registry_validate_metric(
    metric_key: str,
    deep: bool = Query(False, description="深度校验：执行⑤物理列/⑥试跑（依赖数据源）"),
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    repo = _repo(runtime)
    tid = _read_scope(current, None)
    defn = await repo.get_metric_def(metric_key, tenant_id=tid)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
    # 引用域必须是「全局 ∪ 本租户」：租户指标引用全局 measure 是合法的
    all_defs = await repo.list_all_metric_defs(tenant_id=tid)
    measure_keys = await repo.list_all_measure_keys(tenant_id=tid)
    result = await validate_metric_definition(
        defn,
        metric_key=metric_key,
        all_metric_defs=all_defs,
        measure_keys=measure_keys,
        layer=runtime.semantic_layer,
        engine=runtime.metric_engine,
        deep=deep,
    )
    return result.to_dict()


@router.post(
    "/metrics/{metric_key}/submit",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="提交审核（draft → review）",
)
async def registry_submit_metric(
    metric_key: str,
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).submit_metric_review(
            metric_key, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
    return {"metric": item}


@router.post(
    "/metrics/{metric_key}/publish",
    dependencies=[Depends(require_role(ADMIN))],
    summary="审批发布（review → published，仅 admin）",
)
async def registry_publish_metric(
    metric_key: str,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
    deep: bool = Query(False),
) -> Dict[str, Any]:
    repo = _repo(runtime)
    res = await repo.publish_metric(
        metric_key,
        operator=_operator(current),
        approved_by=_operator(current),
        layer=runtime.semantic_layer,
        engine=runtime.metric_engine,
        deep=deep,
        tenant_id=_write_tenant(current),
    )
    if res.get("ok") is False:
        err = res.get("error")
        if err == "not_found":
            raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
        if err == "illegal_transition":
            raise HTTPException(
                status_code=409,
                detail=f"非法状态转移：{res.get('current')} -> published（需先 submit）",
            )
        raise HTTPException(status_code=422, detail=res.get("validation"))
    # PRD 5.9 闭环：发布成功后 best-effort 重建口径字典，使新指标进入知识库（不阻断发布本身）
    try:
        from dmf_agent.semantic.kb_rebuild import rebuild_dictionary_for_metric

        kb = await rebuild_dictionary_for_metric(
            runtime=runtime,
            metric_brief=res["metric"],
            tenant_id=_write_tenant(current),
        )
        res["kb_rebuild"] = kb
    except Exception as exc:  # noqa: BLE001
        res["kb_rebuild"] = {"ok": False, "error": str(exc)}
    return res


@router.post(
    "/metrics/{metric_key}/deprecate",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="废弃指标（published → deprecated）",
)
async def registry_deprecate_metric(
    metric_key: str,
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).deprecate_metric(
            metric_key, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"指标不存在：{metric_key}")
    return {"metric": item}


@router.get(
    "/metrics/{metric_key}/versions",
    dependencies=_guard(PERM_SEMANTIC),
    summary="指标版本历史（含口径快照）",
)
async def registry_metric_versions(
    metric_key: str,
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items = await _repo(runtime).list_metric_versions(
        metric_key, tenant_id=_read_scope(current, None)
    )
    return {"items": items, "total": len(items)}


@router.post(
    "/metrics/{metric_key}/rollback",
    dependencies=[Depends(require_role(ADMIN))],
    summary="回滚到指定版本（仅 admin，生成新版本而非物理回退）",
)
async def registry_rollback_metric(
    metric_key: str,
    body: RollbackRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    item = await _repo(runtime).rollback_metric(
        metric_key,
        body.version,
        operator=_operator(current),
        approved_by=_operator(current),
        tenant_id=_write_tenant(current),
    )
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"指标或版本不存在：{metric_key} v{body.version}"
        )
    return {"metric": item}


# ============================================================================
# 写路径：维度（与指标对称：草稿 / 编辑 / 校验 / 提交 / 发布 / 废弃 / 版本 / 回滚）
# ============================================================================


@router.post(
    "/dimensions/draft",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="新建维度草稿",
)
async def registry_create_dimension_draft(
    body: Dict[str, Any],
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).create_dimension_draft(
            body, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"dimension": item}


@router.put(
    "/dimensions/{dimension_key}",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="编辑维度（草稿/待审）",
)
async def registry_update_dimension(
    dimension_key: str,
    body: Dict[str, Any],
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).update_dimension(
            dimension_key, body, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"维度不存在：{dimension_key}")
    return {"dimension": item}


@router.post(
    "/dimensions/{dimension_key}/validate",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="校验维度口径（Schema 闸）",
)
async def registry_validate_dimension(
    dimension_key: str,
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    defn = await _repo(runtime).get_dimension_def(
        dimension_key, tenant_id=_read_scope(current, None)
    )
    if defn is None:
        raise HTTPException(status_code=404, detail=f"维度不存在：{dimension_key}")
    result = validate_dimension_definition(defn)
    return result.to_dict()


@router.post(
    "/dimensions/{dimension_key}/submit",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="提交维度审核（draft → review）",
)
async def registry_submit_dimension(
    dimension_key: str,
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).submit_dimension_review(
            dimension_key, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"维度不存在：{dimension_key}")
    return {"dimension": item}


@router.post(
    "/dimensions/{dimension_key}/publish",
    dependencies=[Depends(require_role(ADMIN))],
    summary="审批发布维度（仅 admin）",
)
async def registry_publish_dimension(
    dimension_key: str,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    res = await _repo(runtime).publish_dimension(
        dimension_key,
        operator=_operator(current),
        approved_by=_operator(current),
        tenant_id=_write_tenant(current),
    )
    if res.get("ok") is False:
        err = res.get("error")
        if err == "not_found":
            raise HTTPException(status_code=404, detail=f"维度不存在：{dimension_key}")
        if err == "illegal_transition":
            raise HTTPException(
                status_code=409,
                detail=f"非法状态转移：{res.get('current')} -> published（需先 submit）",
            )
        raise HTTPException(status_code=422, detail=res.get("validation"))
    return res


@router.post(
    "/dimensions/{dimension_key}/deprecate",
    dependencies=_guard(PERM_SEMANTIC_WRITE),
    summary="废弃维度（published → deprecated）",
)
async def registry_deprecate_dimension(
    dimension_key: str,
    current: CurrentUser = Depends(require_permission(PERM_SEMANTIC_WRITE)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    try:
        item = await _repo(runtime).deprecate_dimension(
            dimension_key, operator=_operator(current), tenant_id=_write_tenant(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail=f"维度不存在：{dimension_key}")
    return {"dimension": item}


@router.get(
    "/dimensions/{dimension_key}/versions",
    dependencies=_guard(PERM_SEMANTIC),
    summary="维度版本历史（含口径快照）",
)
async def registry_dimension_versions(
    dimension_key: str,
    current: Optional[CurrentUser] = Depends(require_permission(PERM_SEMANTIC)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    items = await _repo(runtime).list_dimension_versions(
        dimension_key, tenant_id=_read_scope(current, None)
    )
    return {"items": items, "total": len(items)}


@router.post(
    "/dimensions/{dimension_key}/rollback",
    dependencies=[Depends(require_role(ADMIN))],
    summary="回滚到指定维度版本（仅 admin）",
)
async def registry_rollback_dimension(
    dimension_key: str,
    body: RollbackRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    item = await _repo(runtime).rollback_dimension(
        dimension_key,
        body.version,
        operator=_operator(current),
        approved_by=_operator(current),
        tenant_id=_write_tenant(current),
    )
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"维度或版本不存在：{dimension_key} v{body.version}"
        )
    return {"dimension": item}
