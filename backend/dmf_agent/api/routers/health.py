"""健康检查。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dmf_agent import __version__
from dmf_agent.api.deps import get_runtime
from dmf_agent.services.runtime import AppRuntime

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    components: dict
    time: str


def _embedding_component(rt: AppRuntime) -> str:
    """由探活状态归纳嵌入服务健康度：ok / down / unknown / disabled。"""
    st = getattr(rt, "embedding_status", None) or {}
    mode = st.get("mode")
    if not mode or mode == "disabled":
        return "disabled"
    reachable = st.get("reachable")
    if reachable is None:
        return "unknown"
    return "ok" if reachable else "down"


def _starrocks_component(rt: AppRuntime) -> str:
    """取数数据源状态：configured / unconfigured / disabled（不在此处做网络探测）。

    真实连通性探测见 GET /api/v1/semantic/health，避免 /health 被慢连接拖住。
    """
    if getattr(rt, "metric_engine", None) is None:
        return "disabled"
    return "configured" if rt.settings.effective_starrocks.configured else "unconfigured"


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(runtime: AppRuntime = Depends(get_runtime)) -> HealthResponse:
    s = runtime.settings
    embedding_state = _embedding_component(runtime)
    # LLM 状态：优先库内默认模型（模型管理生效覆盖），否则环境变量配置
    effective = runtime.model_registry.effective_llm if runtime.model_registry else None
    llm_state = "configured" if (effective is not None or s.llm.chat_configured) else "mock"
    components = {
        "llm": llm_state,
        "database": runtime.db.mode,  # mysql | memory
        "rag": runtime.rag_backend,
        "embedding": embedding_state,  # ok | down | unknown | disabled
        "starrocks": _starrocks_component(runtime),  # 主流程取数数据源
        "trace_store": runtime.db.mode,
        # 观测：遵循 OpenTelemetry，span 落库（无外部 collector 导出）
        "otel_tracing": "on" if s.observability.enabled else "off",
    }
    # 嵌入服务不可达时整体状态仍为 ok（应用可降级 BM25），但 components 中标注 down 便于排查
    return HealthResponse(
        status="ok",
        version=__version__,
        components=components,
        time=datetime.now().isoformat(),
    )
