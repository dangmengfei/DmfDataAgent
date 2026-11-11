"""Embedding 服务探活（探针 + 看门狗）。

DmfDataAgent 通过 HTTP 调用独立部署的 embedding-service（OpenAI 兼容 /v1/embeddings）。
本模块负责：

- ``probe_embedding_service``：一次性探活，返回可达状态 + 详情（如模型名）。
- ``embedding_watchdog``：在应用 lifespan 中启动的后台协程，周期性重探 embedding 服务，
  并把状态写回 ``AppRuntime.embedding_status``；**状态发生翻转（可用<->不可用）时打印告警/恢复日志**。

设计原则：应用不依赖 embedding 服务（向量不可用时自动降级 BM25），因此探活失败**只告警、不阻断启动**。
探活无外部依赖（仅用标准库 urllib + asyncio.to_thread），不引入新包。

健康检查端点位置：embedding-service 的 ``/health`` 挂在根路径（如 ``http://embedding:8001/health``），
而应用配置的 ``EMBEDDING_BASE_URL`` 指向 ``/v1`` 前缀（``http://embedding:8001/v1``），故需剥离尾部的 ``/v1``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# 看门狗重探间隔（秒）
_WATCHDOG_INTERVAL = 30
# 单次探活超时（秒）
_PROBE_TIMEOUT = 3.0


def _service_root(base_url: Optional[str]) -> Optional[str]:
    """由 EMBEDDING_BASE_URL 推导出 embedding-service 根地址（剥离尾部的 /v1）。"""
    if not base_url:
        return None
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root or None


def _probe_sync(root: str, timeout: float) -> dict:
    """同步探活（在线程中执行，避免阻塞事件循环）。"""
    url = f"{root}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                try:
                    data = json.loads(resp.read().decode())
                except (ValueError, UnicodeDecodeError):
                    data = {}
                return {
                    "reachable": True,
                    "mode": "openai",
                    "detail": "ok",
                    "model": data.get("model"),
                }
            return {
                "reachable": False,
                "mode": "openai",
                "detail": f"HTTP {resp.status}",
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "reachable": False,
            "mode": "openai",
            "detail": f"{type(exc).__name__}: {exc}",
        }


async def probe_embedding_service(base_url: Optional[str], timeout: float = _PROBE_TIMEOUT) -> dict:
    """一次性探活 embedding-service，返回状态字典。

    返回示例::

        {"reachable": True,  "mode": "openai", "detail": "ok", "model": "BAAI/bge-large-zh"}
        {"reachable": False, "mode": "openai", "detail": "ConnectError: ..."}
        {"reachable": False, "mode": "disabled", "detail": "EMBEDDING_BASE_URL 未配置"}
    """
    root = _service_root(base_url)
    if not root:
        return {
            "reachable": False,
            "mode": "disabled",
            "detail": "EMBEDDING_BASE_URL 未配置（backend 非 openai 或嵌入服务地址为空）",
        }
    return await asyncio.to_thread(_probe_sync, root, timeout)


async def embedding_watchdog(runtime, interval: int = _WATCHDOG_INTERVAL) -> None:
    """后台看门狗：周期性探活 embedding-service，翻转时告警/恢复。

    仅在 ``EMBEDDING_BACKEND=openai`` 且已配置时工作；否则直接标记 disabled 并退出。
    状态持续写入 ``runtime.embedding_status``，供 ``/health`` 与日志使用。
    """
    base_url = runtime.settings.llm.embedding_base_url
    if runtime.settings.llm.embedding_backend != "openai" or not base_url:
        runtime.embedding_status = {
            "reachable": False,
            "mode": "disabled",
            "detail": "backend!=openai，无需探活嵌入服务（本地/未配置）",
        }
        return

    last_reachable: Optional[bool] = None
    while True:
        status = await probe_embedding_service(base_url)
        runtime.embedding_status = status
        reachable = status["reachable"]

        if last_reachable is None:
            # 首次（启动）探活：不可达即打印启动告警
            if reachable:
                logger.info("嵌入服务探活通过：%s (model=%s)", base_url, status.get("model"))
            else:
                logger.error(
                    "🚨 启动告警：嵌入服务不可达，RAG 向量召回将降级 BM25：%s | %s",
                    base_url,
                    status["detail"],
                )
        elif reachable != last_reachable:
            if reachable:
                logger.warning("✅ 嵌入服务已恢复可用：%s", base_url)
            else:
                logger.error(
                    "🚨 告警：嵌入服务不可用，RAG 向量召回降级 BM25：%s | %s",
                    base_url,
                    status["detail"],
                )

        last_reachable = reachable
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("嵌入服务看门狗已停止")
            break
