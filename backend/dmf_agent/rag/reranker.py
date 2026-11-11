"""重排客户端（FR7 知识库检索第二阶段：cross-encoder 精排）。

设计要点
--------
1. **与嵌入服务同源**：应用进程内**不加载**任何模型（保持后端轻量、可水平扩展），
   重排模型由 embedding-service 承载，本模块只做 HTTP 调用。生产环境若使用商用
   rerank API（Cohere / Jina / BGE-reranker 服务），只需把 `DMF_RAG-RERANK_BASE_URL`
   指过去——契约对齐常见的 `{query, documents} -> [{index, relevance_score}]`。
2. **优雅降级**：任何失败（未配置 / 连接失败 / 超时 / 非 200 / 响应不合法）都返回
   `None`，调用方保持 RRF 融合顺序，**知识问答不会因重排不可用而失败**。
3. **有界延迟**：批量一次请求（候选一次性送进去），超时由 `DMF_RAG-RERANK_TIMEOUT` 控制。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import httpx

from dmf_agent.config.settings import Settings

logger = logging.getLogger(__name__)


class RerankerClient:
    """重排服务 HTTP 客户端（无状态、可重入）。"""

    def __init__(
        self,
        base_url: str,
        model: str = "bge-reranker-base",
        api_key: str = "",
        timeout: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        # 就绪状态：None=未探测 / True=可用 / False=不可用（探测到失败后置 False，
        # 避免每次检索都白等一个超时；显式成功调用会重新置 True）
        self.available: Optional[bool] = None

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/rerank"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def rerank(
        self, query: str, documents: List[str], top_n: Optional[int] = None
    ) -> Optional[List[Tuple[int, float]]]:
        """对候选重排，返回 [(原始下标, 相关性 0~1)] 降序；失败返回 None。"""
        if not documents or not query.strip():
            return []
        payload: dict = {"model": self._model, "query": query, "documents": documents}
        if top_n:
            payload["top_n"] = top_n
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self.endpoint, json=payload, headers=self._headers()
                )
            if resp.status_code != 200:
                # 503 = 服务在但模型未就位（可识别、无需重试）
                logger.warning(
                    "重排服务返回 %s，降级为 RRF 顺序：%s",
                    resp.status_code,
                    str(resp.text)[:200],
                )
                self.available = False
                return None
            data = resp.json()
            results = data.get("results")
            if not isinstance(results, list):
                logger.warning("重排响应缺少 results 字段，降级为 RRF 顺序")
                self.available = False
                return None
            pairs: List[Tuple[int, float]] = []
            for item in results:
                try:
                    idx = int(item["index"])
                    score = float(item.get("relevance_score", 0.0))
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= idx < len(documents):
                    pairs.append((idx, score))
            self.available = True
            return pairs
        except Exception as exc:  # noqa: BLE001 - 重排失败必须降级，不能影响问答
            logger.warning("重排调用失败，降级为 RRF 顺序：%s", exc)
            self.available = False
            return None

    async def probe(self) -> bool:
        """探活：用一次极小请求判断服务是否可用（供 /health 与看门狗使用）。"""
        result = await self.rerank("探活", ["探活"])
        return result is not None


def build_reranker(settings: Optional[Settings] = None) -> Optional[RerankerClient]:
    """按配置构建重排客户端；未启用或无法确定地址时返回 None（= 不重排）。"""
    s = settings
    if s is None:
        from dmf_agent.config.settings import get_settings

        s = get_settings()
    rag = s.rag
    if not rag.rerank_enabled:
        return None
    base_url = (rag.rerank_base_url or "").strip()
    if not base_url:
        # 默认复用嵌入服务：http://host:8001/v1 -> http://host:8001/v1/rerank
        embedding_url = (s.llm.embedding_base_url or "").strip()
        if not embedding_url:
            logger.info("未配置重排地址且嵌入服务地址为空，重排不可用")
            return None
        base_url = embedding_url
    return RerankerClient(
        base_url=base_url,
        model=rag.rerank_model,
        api_key=rag.rerank_api_key or s.llm.embedding_api_key or "",
        timeout=rag.rerank_timeout,
    )
