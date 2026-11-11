"""Few-Shot 样本管理 API（管理面「Few-Shot 管理」页）。

few-shot 样本 = 意图识别阶段注入 prompt 的「少样本示例」，来源两类共存在
`few_shot_examples` 一张表里（由 `source` 区分）：

- `like`   用户点赞助手回答自动沉淀（带 session_id / message_id 溯源，重复点赞累加 score）
- `manual` 管理面手工录入 / 批量导入

因此**页面上列出的样本，就是真正参与 BM25 召回的候选集合**（召回侧只额外排除
`status=disabled`）。"试召回" (`POST /fewshots/recall`) 直接复用
`memory/retriever.py::MemoryRetriever.recall`，与线上意图识别注入同源同口径。

接口：
- `GET    /fewshots`              列表（类别 / 意图 / 来源 / 状态 / 关键词 + 分页）
- `GET    /fewshots/stats`        统计卡（总量 / 启用 / 停用 / 点赞 / 手工）
- `GET    /fewshots/facets`       已用类别与意图去重清单（过滤下拉选项）
- `POST   /fewshots`              手工新增
- `POST   /fewshots/recall`       试召回（返回命中样本 + 实际注入文本）
- `GET    /fewshots/export`       导出 JSON（前端落盘，不经新窗口，避免鉴权头丢失）
- `POST   /fewshots/import`       批量导入（导出同构；兼容旧 experience JSON 的 query/content）
- `GET    /fewshots/{item_id}`    详情
- `PUT    /fewshots/{item_id}`    编辑（含停用 / 启用）
- `DELETE /fewshots/{item_id}`    删除

权限：读 `fewshot`，写 `fewshot:write`（viewer 无，前端入口不渲染 + 接口 403 双重）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission
from dmf_agent.auth.roles import PERM_FEWSHOT, PERM_FEWSHOT_WRITE
from dmf_agent.memory.retriever import MemoryRetriever
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.security.export_gate import (
    apply_export_governance,
    assert_export_allowed,
    audit_export,
)
from dmf_agent.schemas.feedback import (
    FewShotData,
    FewShotImportRequest,
    FewShotImportResult,
    FewShotPatch,
    FewShotRecallHit,
    FewShotRecallRequest,
    FewShotRecallResult,
    FewShotUpsert,
)
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fewshots", tags=["fewshots"])

# 导出文件里的版本号：便于将来变更结构时识别
EXPORT_VERSION = 1


def _guard(permission: str) -> list:
    """路由级权限点依赖（admin 通配放行；鉴权关闭时合成 admin 亦放行）。"""
    return [Depends(require_permission(permission))]


# ---------- 列表 / 统计 / 维度值 ----------


@router.get("", dependencies=_guard(PERM_FEWSHOT), summary="Few-Shot 样本列表")
async def list_few_shots(
    category: Optional[str] = Query(None),
    intent: Optional[str] = Query(None),
    source: Optional[str] = Query(None, pattern="^(like|manual)$"),
    status: Optional[str] = Query(None, pattern="^(active|disabled)$"),
    keyword: str = Query("", description="匹配问题 / 答案 / 标签"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """样本列表（默认按创建时间倒序）；返回 {items, total}。"""
    items, total = await runtime.db.few_shot_repo.query(
        category=category, intent=intent, source=source, status=status,
        keyword=keyword, limit=limit, offset=offset,
    )
    return {
        "items": [i.model_dump() for i in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/stats", dependencies=_guard(PERM_FEWSHOT), summary="Few-Shot 样本统计"
)
async def few_shot_stats(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    db = runtime.db

    async def _safe(coro, default=0):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - 统计失败不该把页面拖垮
            logger.warning("Few-Shot 统计失败（按 0 计）：%s", exc)
            return default

    total = await _safe(db.few_shot_repo.count())
    active = await _safe(db.few_shot_repo.count(status="active"))
    return {
        "total": total,
        "active": active,
        "disabled": (total - active) if isinstance(total, int) and isinstance(active, int) else 0,
        "like": await _safe(db.few_shot_repo.count(source="like")),
        "manual": await _safe(db.few_shot_repo.count(source="manual")),
    }


@router.get(
    "/facets",
    dependencies=_guard(PERM_FEWSHOT),
    summary="已用类别 / 意图 / 标签清单（过滤下拉选项）",
)
async def few_shot_facets(runtime: AppRuntime = Depends(get_runtime)) -> Dict[str, Any]:
    """从现有样本里汇总去重维度值，避免前端硬编码枚举与库内数据不一致。"""
    items = await runtime.db.few_shot_repo.list_all()
    cats = sorted({i.category for i in items if i.category})
    intents = sorted({i.intent for i in items if i.intent})
    tags: set = set()
    for i in items:
        for t in (i.tags or "").split(","):
            t = t.strip()
            if t:
                tags.add(t)
    return {"categories": cats, "intents": intents, "tags": sorted(tags)}


@router.get(
    "/export", dependencies=_guard(PERM_FEWSHOT), summary="导出样本（JSON）"
)
async def export_few_shots(
    status: Optional[str] = Query(None, pattern="^(active|disabled)$"),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> Dict[str, Any]:
    """导出为 JSON（前端用 Blob 落盘）。

    刻意**不做成文件流 + 新窗口下载**：浏览器直开新窗口不会带 Authorization 头，
    在鉴权开启时必然 401（`/obs/export` 就踩过这个坑）。这里返回 JSON 由前端保存。

    FR13 治理：先过审批闸门（越权时不查库）→ 行数按 `export_max_rows` 截断 →
    返回体带 `watermark`（导出人/租户/时间/行数）→ 写导出审计。
    """
    assert_export_allowed(runtime, current, kind="fewshot")
    items, watermark = apply_export_governance(
        runtime, current, kind="fewshot", rows=await runtime.db.few_shot_repo.list_all(status=status)
    )
    await audit_export(
        runtime, current, kind="fewshot", rows=len(items),
        truncated=bool(watermark and watermark.get("truncated")),
    )
    return {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "watermark": watermark,
        "items": [
            {
                "question": i.question,
                "answer": i.answer,
                "intent": i.intent,
                "category": i.category,
                "tags": i.tags,
                "weight": i.weight,
                "status": i.status,
                "source": i.source,
            }
            for i in items
        ],
    }


# ---------- 写入 ----------


@router.post(
    "",
    response_model=FewShotData,
    dependencies=_guard(PERM_FEWSHOT_WRITE),
    summary="新增一条手工样本",
)
async def create_few_shot(
    body: FewShotUpsert,
    runtime: AppRuntime = Depends(get_runtime),
) -> FewShotData:
    """手工录入样本（source=manual）。

    同一 (question, category) 已存在时按 409 拒绝 —— 样本库的价值在于"每个问题一条
    最可信示例"，静默追加会让同一问题在 top_k 里互相挤占（召回侧虽有去重，但让用户
    明确知道"已经有了"比默默合并更好）。
    """
    existing = await runtime.db.few_shot_repo.find_by_question(
        body.question.strip(), body.category
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"同类目下已存在相同问题的样本（id={existing.id}，来源={existing.source}）",
        )
    created = await runtime.db.few_shot_repo.add(
        FewShotData(
            id=0,
            session_id="",
            message_id="",
            intent=body.intent,
            category=body.category,
            question=body.question.strip(),
            answer=body.answer,
            score=1,
            source="manual",
            status="active",
            weight=body.weight,
            tags=body.tags,
        )
    )
    logger.info("新增手工 few-shot 样本：id=%s category=%s", created.id, created.category)
    return created


@router.post(
    "/recall",
    response_model=FewShotRecallResult,
    dependencies=_guard(PERM_FEWSHOT),
    summary="试召回（验证样本是否会被命中）",
)
async def recall_few_shots(
    body: FewShotRecallRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> FewShotRecallResult:
    retriever = MemoryRetriever(
        runtime.db.few_shot_repo, runtime.db.domain_memory_repo, top_k=body.top_k
    )
    outcome = await retriever.recall(body.query, top_k=body.top_k)
    if body.fail_on_empty and not outcome.hits:
        raise HTTPException(
            status_code=404,
            detail=f"无样本命中（候选 {outcome.candidates} 条）",
        )
    return FewShotRecallResult(
        query=outcome.query,
        top_k=body.top_k,
        candidates=outcome.candidates,
        injected_text=outcome.injected_text,
        hits=[
            FewShotRecallHit(
                id=h.id,
                source=h.source,
                question=h.question,
                answer=h.answer,
                intent=h.intent,
                category=h.category,
                tags=h.tags,
                weight=h.weight,
                score=h.score,
                bm25=h.bm25,
                rerank=h.rerank,
            )
            for h in outcome.hits
        ],
    )


@router.post(
    "/import",
    response_model=FewShotImportResult,
    dependencies=_guard(PERM_FEWSHOT_WRITE),
    summary="批量导入样本",
)
async def import_few_shots(
    body: FewShotImportRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> FewShotImportResult:
    """导入 JSON 样本（导出同构；兼容旧 experience JSON 的 `query` / `content` 字段）。

    重复处理：同 (question, category) 已存在时 —— `overwrite=true` 覆盖答案 / 意图 /
    标签 / 权重，否则计入 skipped（默认不覆盖，避免导入把人工修正冲掉）。
    """
    result = FewShotImportResult()
    repo = runtime.db.few_shot_repo
    for idx, raw in enumerate(body.items):
        if not isinstance(raw, dict):
            result.errors.append(f"第 {idx + 1} 条不是对象，已跳过")
            continue
        # 兼容两种字段命名：question/answer（导出）与 query/content（旧经验库 JSON）
        question = str(raw.get("question") or raw.get("query") or "").strip()
        answer = str(raw.get("answer") or raw.get("content") or "")
        if not question:
            result.errors.append(f"第 {idx + 1} 条缺少 question/query，已跳过")
            continue
        category = str(raw.get("category") or body.category or "intention")
        intent = raw.get("intent")
        tags = str(raw.get("tags") or "")
        try:
            weight = float(raw.get("weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        status = str(raw.get("status") or "active")
        if status not in ("active", "disabled"):
            status = "active"

        existing = await repo.find_by_question(question, category)
        if existing is not None:
            if not body.overwrite:
                result.skipped += 1
                continue
            await repo.update(
                existing.id, answer=answer, intent=intent, tags=tags,
                weight=weight, status=status,
            )
            result.updated += 1
            continue
        await repo.add(
            FewShotData(
                id=0, session_id="", message_id="", intent=intent, category=category,
                question=question, answer=answer, score=1, source="manual",
                status=status, weight=weight, tags=tags,
            )
        )
        result.created += 1
    logger.info(
        "导入 few-shot 样本：created=%s updated=%s skipped=%s errors=%s",
        result.created, result.updated, result.skipped, len(result.errors),
    )
    return result


# ---------- 单条（detail / update / delete，须声明在 /stats 等静态路径之后）----------


@router.get(
    "/{item_id}", response_model=FewShotData, dependencies=_guard(PERM_FEWSHOT),
    summary="样本详情",
)
async def get_few_shot(
    item_id: int, runtime: AppRuntime = Depends(get_runtime)
) -> FewShotData:
    item = await runtime.db.few_shot_repo.get_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return item


@router.put(
    "/{item_id}", response_model=FewShotData, dependencies=_guard(PERM_FEWSHOT_WRITE),
    summary="编辑样本（含停用 / 启用）",
)
async def update_few_shot(
    item_id: int,
    body: FewShotPatch,
    runtime: AppRuntime = Depends(get_runtime),
) -> FewShotData:
    """部分更新：只传需要改的字段（`status=disabled` 即停用，不参与召回但保留记录）。"""
    fields = body.model_dump(exclude_none=True)
    if "question" in fields:
        fields["question"] = str(fields["question"]).strip()
    updated = await runtime.db.few_shot_repo.update(item_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return updated


@router.delete(
    "/{item_id}", dependencies=_guard(PERM_FEWSHOT_WRITE), summary="删除样本"
)
async def delete_few_shot(
    item_id: int, runtime: AppRuntime = Depends(get_runtime)
) -> Dict[str, Any]:
    deleted = await runtime.db.few_shot_repo.delete(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="样本不存在")
    logger.info("删除 few-shot 样本：id=%s", item_id)
    return {"deleted": True, "id": item_id}
