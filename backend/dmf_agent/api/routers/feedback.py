"""点赞 / 点踩反馈接口。

- POST /feedback：提交对某条助手消息的反馈。
  - like    -> 写入 few_shot_examples（点赞样本库，意图识别阶段 BM25 召回注入 prompt）
  - dislike -> 写入 bad_cases（bad case 库，当前仅落库供后续分析 / 回流）
  助手消息 id（= trace_records.round_id）可反查 messages 表拿到 question / answer，
  再反查 trace_records 拿到 intent（最权威），回退到 messages.node_trace.intent。
- GET /feedback/status?message_ids=a,b,c：批量查询消息反馈状态，供历史消息回显高亮。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dmf_agent.api.deps import get_runtime
from dmf_agent.schemas.feedback import (
    BadCaseData,
    FeedbackRequest,
    FeedbackStatusResponse,
    FewShotData,
)
from dmf_agent.schemas.memory import DomainMemoryData, UserPreferenceData
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])

# 点赞时可从本轮查询要素沉淀为 L2 偏好的字段（键名对齐 _apply_preference_defaults）
_LEARNABLE_PREF_KEYS = ("granularity", "dimensions")
# 系统推断的偏好置信度（低于用户显式设置 1.0；用户显式设置会覆盖同键记录）
_INFERRED_CONFIDENCE = 0.6


async def _learn_preferences(db, session_id: str, msg) -> List[str]:
    """点赞时把本轮口径沉淀为用户偏好（L2，source=inferred）。

    偏好归属**会话所有者**（而非当前操作者）：偏好是"这个人做分析的稳定口径"，
    管理员查看他人会话并点赞不应把口径写到管理员名下。
    仅沉淀非空且未存在的键，避免每次点赞都把同一偏好重写一遍。
    """
    elements = (msg.node_trace or {}).get("query_elements") or {}
    if not elements:
        return []
    session = await db.session_repo.get(session_id)
    if session is None or not session.user_id:
        return []
    candidates: dict[str, str] = {}
    granularity = (elements.get("time_range") or {}).get("granularity")
    if granularity:
        candidates["granularity"] = str(granularity)
    dims = elements.get("dimensions") or []
    if dims:
        candidates["dimensions"] = ",".join(str(d) for d in dims)

    learned: List[str] = []
    for key in _LEARNABLE_PREF_KEYS:
        value = candidates.get(key)
        if not value:
            continue
        try:
            existing = await db.preference_repo.get(session.user_id, key)
            # 用户显式设置过的偏好不被系统推断覆盖
            if existing is not None and existing.source == "user":
                continue
            if existing is not None and existing.value == value:
                continue
            await db.preference_repo.upsert(
                UserPreferenceData(
                    id=0,
                    user_id=session.user_id,
                    tenant_id=session.tenant_id or "",
                    key=key,
                    value=value,
                    source="inferred",
                    confidence=_INFERRED_CONFIDENCE,
                    status="active",
                )
            )
            learned.append(f"{key}={value}")
        except Exception as exc:  # noqa: BLE001 - 偏好沉淀失败不影响点赞主流程
            logger.warning("点赞沉淀 L2 偏好失败（跳过）key=%s：%s", key, exc)
    return learned


@router.post("")
async def submit_feedback(
    req: FeedbackRequest,
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    db = runtime.db
    msg = await db.message_repo.get(req.message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="消息不存在")

    session_id = req.session_id or msg.session_id
    question = (msg.node_trace or {}).get("question") or ""
    answer = msg.content or ""

    # 取消反馈：再点一次同类型 = 撤销（删样本；like 撤销同步清 L3 few-shot 副本）
    if req.type == "cancel":
        removed = await db.few_shot_repo.delete_by_message(req.message_id)
        if removed:
            for item in await db.domain_memory_repo.list_all():
                if item.type == "few_shot" and item.question == question and item.answer == answer:
                    try:
                        await db.domain_memory_repo.delete(item.id)
                    except Exception as exc:  # noqa: BLE001 - L3 清理失败不影响取消主流程
                        logger.warning("取消点赞清理 L3 副本失败（跳过）：%s", exc)
        await db.bad_case_repo.delete_by_message(req.message_id)
        logger.info("取消反馈：message_id=%s", req.message_id)
        return {"ok": True, "type": "cancel", "message_id": req.message_id}

    # 意图优先取自 trace_records（最权威），回退到 messages.node_trace.intent
    intent: Optional[str] = None
    try:
        trace_rec = await db.trace_repo.get(session_id, req.message_id)
        if trace_rec is not None:
            intent = trace_rec.intent
    except Exception as exc:  # noqa: BLE001 - 观测查询失败不影响反馈落库
        logger.warning("查询 trace 意图失败，回退 node_trace.intent：%s", exc)
    if not intent:
        intent = (msg.node_trace or {}).get("intent")

    if req.type == "like":
        # 切换反馈：此前点过踩则先撤 bad-case，保证一消息仅一种状态
        await db.bad_case_repo.delete_by_message(req.message_id)
        saved = await db.few_shot_repo.add(
            FewShotData(
                id=0,
                session_id=session_id,
                message_id=req.message_id,
                intent=intent,
                category="intention",
                question=question,
                answer=answer,
                score=1,
                # 标记来源：与「Few-Shot 管理」页手工录入的样本（manual）区分展示
                source="like",
                status="active",
            )
        )
        # 同步沉淀 L3 领域知识（已验证样本视图）：type=few_shot / status=approved，
        # 意图识别阶段与 few-shot 库合并做 BM25 召回注入 prompt。
        try:
            await db.domain_memory_repo.add(
                DomainMemoryData(
                    id=0,
                    domain="metric_query",
                    type="few_shot",
                    question=question,
                    answer=answer,
                    intent=intent,
                    weight=1.0,
                    status="approved",
                    created_by=session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - L3 沉淀失败不影响点赞主流程
            logger.warning("点赞沉淀 L3 失败（跳过）：%s", exc)
        # 同步沉淀 L2 用户偏好：点赞=认可本轮口径，把该轮的粒度 / 常用维度记为
        # source='inferred'（confidence 0.6，管理面可改可删；用户显式设置会覆盖它）。
        learned = await _learn_preferences(db, session_id, msg)
        logger.info("点赞入库 few-shot：message_id=%s intent=%s", req.message_id, intent)
        return {
            "ok": True,
            "type": "like",
            "message_id": req.message_id,
            "example_id": saved.id,
            "learned_preferences": learned,
        }

    # 切换反馈：此前点过赞则先撤 few-shot 与其 L3 副本，保证一消息仅一种状态
    if await db.few_shot_repo.delete_by_message(req.message_id):
        for item in await db.domain_memory_repo.list_all():
            if item.type == "few_shot" and item.question == question and item.answer == answer:
                try:
                    await db.domain_memory_repo.delete(item.id)
                except Exception as exc:  # noqa: BLE001 - L3 清理失败不影响点踩主流程
                    logger.warning("切换反馈清理 L3 副本失败（跳过）：%s", exc)
    saved = await db.bad_case_repo.add(
        BadCaseData(
            id=0,
            session_id=session_id,
            message_id=req.message_id,
            intent=intent,
            question=question,
            answer=answer,
            reason=req.reason,
        )
    )
    logger.info("点踩入库 bad-case：message_id=%s intent=%s", req.message_id, intent)
    return {
        "ok": True,
        "type": "dislike",
        "message_id": req.message_id,
        "case_id": saved.id,
    }


@router.get("/status", response_model=FeedbackStatusResponse)
async def feedback_status(
    message_ids: str = Query(..., description="逗号分隔的消息 id 列表"),
    runtime: AppRuntime = Depends(get_runtime),
) -> FeedbackStatusResponse:
    db = runtime.db
    ids = [x for x in message_ids.split(",") if x.strip()]
    status: dict[str, str] = {}
    for mid in ids:
        fs = await db.few_shot_repo.get_by_message(mid)
        if fs is not None:
            status[mid] = "like"
            continue
        bc = await db.bad_case_repo.get_by_message(mid)
        if bc is not None:
            status[mid] = "dislike"
    return FeedbackStatusResponse(status=status)
