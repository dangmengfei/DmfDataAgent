"""零依赖定时调度（Task C）：周期性重建口径字典，使 RAG 权威源与语义层口径保持同步。

不引入 apscheduler 等第三方依赖，直接用 asyncio 周期任务实现。仅当
``settings.rag.kb_auto_refresh_enabled`` 且 ``interval_s`` 合法时启用；向量库或语义层
缺失时静默不启用。周期任务 best-effort：单次重建异常仅告警，不影响下一轮。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def rebuild_dictionary_for_tenants(
    settings, db, vector_store, semantic_layer
) -> Dict[str, Any]:
    """重建全部目标租户的口径字典，返回 ``{tenant_id: chunks}``。best-effort。"""
    from dmf_agent.rag.dictionary import seed_dictionary

    tenant_ids = [""]
    if getattr(getattr(settings, "auth", None), "enabled", False):
        try:
            tenants = await db.tenant_repo.list_all()
            tenant_ids = [t.id for t in tenants] or [""]
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取租户列表失败，口径字典按无租户重建：%s", exc)
            tenant_ids = [""]

    results: Dict[str, Any] = {}
    for tid in tenant_ids:
        try:
            res = await seed_dictionary(
                document_repo=db.document_repo,
                chunk_repo=db.chunk_repo,
                vector_store=vector_store,
                settings=settings,
                layer=semantic_layer,
                tenant_id=tid,
            )
            results[tid] = res.get("chunks")
            logger.info(
                "定时重建口径字典完成（tenant=%s, %s 分块）", tid, res.get("chunks")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("定时重建口径字典失败（tenant=%s，跳过）：%s", tid, exc)
    return results


class DictionaryRefreshScheduler:
    """零依赖异步周期调度器：后台周期重建口径字典。

    用法（在 lifespan 内）：:

        scheduler = DictionaryRefreshScheduler()
        scheduler.start(settings, db, vector_store, semantic_layer)
        ...
        # 关闭时
        await scheduler.stop()
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, settings, db, vector_store, semantic_layer) -> bool:
        """启动周期任务。已启用则幂等返回 ``True``；条件不满足返回 ``False``。"""
        if self.running:
            return True
        if not getattr(getattr(settings, "rag", None), "kb_auto_refresh_enabled", False):
            return False
        interval = getattr(getattr(settings, "rag", None), "kb_auto_refresh_interval_s", 0)
        if not isinstance(interval, int) or interval <= 0:
            return False
        if vector_store is None or semantic_layer is None:
            return False

        self._stop = asyncio.Event()

        async def _loop() -> None:
            try:
                while not self._stop.is_set():
                    # 等待 stop 信号或 interval 到期；stop 优先，保证优雅关闭即时退出
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=interval)
                        break  # stop 被设置，正常退出
                    except asyncio.TimeoutError:
                        pass  # 到点，执行重建
                    if self._stop.is_set():
                        break
                    try:
                        await rebuild_dictionary_for_tenants(
                            settings, db, vector_store, semantic_layer
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "口径字典周期重建异常（下一轮重试）：%s", exc
                        )
            except asyncio.CancelledError:
                raise

        self._task = asyncio.create_task(_loop())
        logger.info("口径字典周期重建已启用（间隔 %s 秒）", interval)
        return True

    async def stop(self) -> None:
        """取消周期任务并等待退出（best-effort）。"""
        task = self._task
        if task is None:
            return
        if self._stop is not None:
            self._stop.set()
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
