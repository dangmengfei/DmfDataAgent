"""发布成功后，把新指标并入语义层副本并重灌口径字典（PRD 5.9 闭环缺口 c）。

为什么单独成模块
----------------
`runtime.semantic_layer` 不含 `SemanticMetricModel` 行，发布那一刻新指标既不进查询层、
也不进知识库口径字典。本模块在「发布成功」之后 best-effort 触发一次口径字典增量重建：
把新指标并入语义层副本，调 `build_dictionary_text` + `seed_dictionary`（权威源，幂等覆盖），
使新口径进入知识库可被检索。

设计原则：best-effort、安全降级。任何异常都转为 `ok=False` 返回，**绝不**阻断「发布成功」本身。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from dmf_agent.rag.dictionary import seed_dictionary
from dmf_agent.rag.vector_store import build_vector_store

logger = logging.getLogger(__name__)


async def rebuild_dictionary_for_metric(
    *,
    runtime,
    metric_brief: Dict[str, Any],
    tenant_id: str = "",
) -> Dict[str, Any]:
    """把新发布指标并入语义层副本，重灌口径字典（权威源），使其进入知识库可被检索。

    返回 ``{"ok": True/False, ...}``；异常一律转 ``ok=False``，不抛出。
    """
    try:
        from dmf_agent.semantic.yaml_defn import build_layer_with_metric

        base = getattr(runtime, "semantic_layer", None)
        merged = build_layer_with_metric(base, metric_brief)
        if merged is None:
            return {"ok": False, "error": "no_merged_layer"}
        settings = getattr(runtime, "settings", None)
        if settings is None:
            return {"ok": False, "error": "no_settings"}
        document_repo = runtime.db.document_repo
        chunk_repo = runtime.db.chunk_repo
        # vector_store 不挂在 runtime 上，按 settings 按需重建（无 embedding 则返回 None，
        # seed_dictionary 对 None 安全：仅落文档/分块，不写向量）。
        vector_store = build_vector_store(settings)
        # 字典按指标归属租户落库；brief 未带 tenant_id 时回落到操作人写入租户。
        seed_tenant = (metric_brief.get("tenant_id") or tenant_id or "")
        res = await seed_dictionary(
            document_repo=document_repo,
            chunk_repo=chunk_repo,
            vector_store=vector_store,
            settings=settings,
            layer=merged,
            tenant_id=seed_tenant,
        )
        return {"ok": True, **res}
    except Exception as exc:  # noqa: BLE001
        logger.warning("口径字典增量重建失败（发布成功不受影响）：%s", exc)
        return {"ok": False, "error": str(exc)}
