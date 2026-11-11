"""LLM 统一工厂（LangChain 适配层）。

迁移说明：替代老系统 llm/ 下十余个自研厂商适配
（openai.py / qwen.py / zhipuai.py / baichuan.py / ernie.py / azure_ml.py / ollama.py ...）。
LangChain 的 ChatOpenAI 走 OpenAI 兼容协议即可覆盖国内主流模型服务：

- qwen     : https://dashscope.aliyuncs.com/compatible-mode/v1
- zhipu    : https://open.bigmodel.cn/api/paas/v4
- deepseek : https://api.deepseek.com/v1
- vllm/自建: http://<host>:<port>/v1
- ollama   : http://localhost:11434/v1

Embedding 同理：本项目把向量化模型单独部署为 embedding-service（OpenAI 兼容 /v1/embeddings 接口），
应用侧设 DMF_LLM-EMBEDDING_BACKEND=openai + DMF_LLM-EMBEDDING_BASE_URL 指向它即可；
上线生产时改 base_url 指向生产小模型服务（同为 OpenAI 兼容）即可，应用代码不变。
开发期不想起独立服务，可设 backend="local" 走进程内 LangChain HuggingFaceEmbeddings。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from dmf_agent.config.settings import LLMSettings, Settings, get_settings

logger = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """向量化主备后端均不可用（如嵌入服务宕机且无本地降级）。"""


class FallbackEmbeddings(Embeddings):
    """主备容错包装：优先用 primary（如调用嵌入服务），失败重试后降级到 secondary（进程内本地模型）。

    - 仅在 primary 持续失败时退到 secondary，且 secondary 可用时才退；
    - 主备都失败则抛 EmbeddingUnavailableError，由上层（检索自动降级 BM25 / 入库接口返回 503）优雅处理。
    用法的两个入口 embed_query / embed_documents 对应 LangChain 单条与批量向量化。
    """

    def __init__(
        self,
        primary: Embeddings,
        secondary: Optional[Embeddings] = None,
        retry: int = 2,
        backoff: float = 0.5,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._retry = max(0, retry)
        self._backoff = backoff

    def _run(self, method: str, *args):
        last = None
        for attempt in range(self._retry + 1):
            try:
                return getattr(self._primary, method)(*args)
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning(
                    "向量化主后端(openai)调用失败(第%d/%d次)：%s",
                    attempt + 1,
                    self._retry + 1,
                    exc,
                )
                if attempt < self._retry:
                    time.sleep(self._backoff * (attempt + 1))
        if self._secondary is not None:
            logger.warning("向量化主后端不可用，降级到进程内本地模型")
            try:
                return getattr(self._secondary, method)(*args)
            except Exception as exc2:  # noqa: BLE001
                logger.error("向量化降级到本地模型也失败：%s", exc2)
                raise EmbeddingUnavailableError(
                    "向量化主备后端均不可用，请检查嵌入服务或本地模型"
                ) from exc2
        raise EmbeddingUnavailableError(
            "向量化主后端不可用且无本地降级，请检查嵌入服务"
        ) from last

    def embed_query(self, text: str) -> list[float]:
        return self._run("embed_query", text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._run("embed_documents", texts)


def get_chat_model(
    settings: Optional[Settings] = None, *, llm_override: Optional[LLMSettings] = None
) -> Optional[BaseChatModel]:
    """按配置构建 ChatModel；未配置时返回 None（上层节点自动降级 mock 模式）。

    ``llm_override``：库内默认模型的 ``LLMSettings`` 覆盖（见 ``services/models.py``），
    非 None 时优先于环境变量配置 —— 模型管理页标记的默认模型由此生效。
    自托管端点（vllm / ollama）常免鉴权，api_key 为空时用 ``"not-needed"`` 占位（与
    Embedding 侧同口径）。
    """
    s = settings or get_settings()
    llm_cfg: LLMSettings = llm_override if llm_override is not None else s.llm
    if llm_override is None and not llm_cfg.chat_configured:
        logger.info("LLM 未配置，Agent 将以 mock 模式运行")
        return None
    if not (llm_cfg.model and llm_cfg.base_url):
        logger.info("LLM 未配置（缺 model/base_url），Agent 将以 mock 模式运行")
        return None

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=llm_cfg.model,
        base_url=llm_cfg.base_url,
        api_key=llm_cfg.api_key or "not-needed",
        temperature=llm_cfg.temperature,
        timeout=llm_cfg.timeout,
        max_retries=llm_cfg.max_retries,
    )


def get_embeddings(settings: Optional[Settings] = None) -> Optional[Embeddings]:
    """按配置构建 Embedding 客户端；未配置时返回 None（RAG 自动降级）。

    - backend="openai" : 【生产/推荐】OpenAI 兼容端点（本项目 embedding-service 或生产小模型服务）。
                        应用通过 HTTP 调用，不要求本地加载模型；api_key 可选。
                        容错：调用失败按 embedding_retry 重试，仍失败则降级到本地模型
                        （embedding_fallback_to_local，需与远端同维度），最终不可用抛 EmbeddingUnavailableError。
    - backend="local"  : 【开发兜底】进程内 LangChain HuggingFaceEmbeddings 本地推理，无需独立服务。
    """
    s = settings or get_settings()
    llm_cfg: LLMSettings = s.llm
    if not llm_cfg.embedding_configured:
        logger.info("Embedding 未配置，RAG 功能将不可用")
        return None

    if llm_cfg.embedding_backend == "local":
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "本地向量化依赖未安装，请执行: pip install -e '.[rag]'"
            ) from exc

        model_kwargs = {"device": llm_cfg.embedding_local_device}
        encode_kwargs = {"normalize_embeddings": llm_cfg.embedding_local_normalize}
        logger.info(
            "使用本地向量化模型: %s (device=%s)",
            llm_cfg.embedding_local_model,
            llm_cfg.embedding_local_device,
        )
        return HuggingFaceEmbeddings(
            model_name=llm_cfg.embedding_local_model,
            cache_folder=llm_cfg.embedding_local_cache_folder,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
        )

    # 外部 OpenAI 兼容端点（本项目 embedding-service / 生产小模型服务）
    from langchain_openai import OpenAIEmbeddings

    primary = OpenAIEmbeddings(
        model=llm_cfg.embedding_model,
        base_url=llm_cfg.embedding_base_url,
        api_key=llm_cfg.embedding_api_key or "not-needed",  # 自托管服务通常免鉴权
        check_embedding_ctx_length=False,
        max_retries=1,  # 重试由 FallbackEmbeddings 统一控制（指数退避）
    )

    # 主后端持续失败时，可选降级到进程内本地模型（需与远端同维度/同模型，
    # 且权重在 app 容器内可用；否则降级本身也会失败，最终由上层优雅处理）
    secondary: Optional[Embeddings] = None
    if llm_cfg.embedding_fallback_to_local and llm_cfg.embedding_local_model:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings

            secondary = HuggingFaceEmbeddings(
                model_name=llm_cfg.embedding_local_model,
                cache_folder=llm_cfg.embedding_local_cache_folder,
                model_kwargs={"device": llm_cfg.embedding_local_device},
                encode_kwargs={"normalize_embeddings": llm_cfg.embedding_local_normalize},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地降级模型不可用，仅保留主后端：%s", exc)

    if secondary is None:
        return primary
    return FallbackEmbeddings(primary, secondary, retry=llm_cfg.embedding_retry)
