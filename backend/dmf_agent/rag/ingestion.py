"""知识入库管道：文档 -> 切分 -> 向量化 -> 持久化（向量库 + MySQL 元数据）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter

from dmf_agent.config.settings import Settings
from dmf_agent.db.base import ChunkRepository, DocumentRepository
from dmf_agent.rag.vector_store import VectorStore
from dmf_agent.schemas.knowledge import ChunkData, DocumentData

logger = logging.getLogger(__name__)

# 中文友好的切分分隔符
_SEPARATORS = ["\n\n", "\n", "。", "；", "！", "？", "，", " ", ""]


def _read_text_lenient(path: Path) -> str:
    """读取纯文本，编码依次尝试 utf-8（含 BOM）→ gb18030（GBK 超集，兼容中文 Windows 文档）。

    都失败时用 utf-8 + replace 兜底（不抛 UnicodeDecodeError），避免上传老编码文档直接 500。
    """
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_file_text(path: Path) -> tuple[str, str]:
    """读取文档文本，返回 (text, source_type)。"""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        return _read_text_lenient(path), "txt" if suffix == ".txt" else "md"
    if suffix == ".pdf":
        # 直接用 pypdf（loaders extra 已声明，轻量、纯 Python），不引入 langchain-community。
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF 加载依赖未安装，请执行: pip install -e '.[loaders]'"
            ) from exc
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if not text.strip():
            raise ValueError("PDF 未解析出任何文本（可能是扫描件 / 图片型 PDF，暂不支持 OCR）")
        return text, "pdf"
    raise ValueError(f"不支持的文件类型：{suffix}（当前支持 txt/md/pdf）")


async def ingest_text(
    title: str,
    text: str,
    document_repo: DocumentRepository,
    chunk_repo: ChunkRepository,
    vector_store: Optional[VectorStore],
    settings: Settings,
    source_type: str = "text",
    source_uri: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
    tenant_id: str = "",
    authority_level: str = "user",
    owner_id: str = "",
) -> DocumentData:
    """入库一段文本；返回更新后的文档记录。

    `tenant_id` 落到文档行上（未归类文档同样带租户），保证检索与列表按租户隔离。
    `owner_id` 落到文档行上（个人/私有库=上传者；企业/公开库或系统权威源=""），
    配合 `tenant_id` 做检索期个人/企业隔离（见 rag/retriever.py）。
    `authority_level`（FR7 来源分级）落到文档行上，检索时按档位加权：
    权威源（口径文档/指标字典）> 沉淀源（点赞审核入库）> 用户上传。
    """
    doc = await document_repo.add(
        DocumentData(
            id="",
            title=title,
            source_type=source_type,
            source_uri=source_uri,
            knowledge_base_id=knowledge_base_id,
            authority_level=authority_level,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
    )
    try:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.rag.chunk_size,
            chunk_overlap=settings.rag.chunk_overlap,
            separators=_SEPARATORS,
        )
        chunks: List[str] = splitter.split_text(text)
        vector_refs: Optional[List[str]] = None
        if vector_store is not None:
            vector_refs = vector_store.add(
                chunks, [{"doc_id": doc.id, "source": title} for _ in chunks]
            )
        else:
            logger.warning("向量库不可用（Embedding 未配置？），仅保存文本分块")

        await chunk_repo.add_many(
            [
                ChunkData(
                    id="",
                    document_id=doc.id,
                    chunk_index=i,
                    content=c,
                    vector_ref=vector_refs[i] if vector_refs else None,
                )
                for i, c in enumerate(chunks)
            ]
        )
        updated = await document_repo.update_status(doc.id, "indexed", chunk_count=len(chunks))
        logger.info("知识入库完成：%s（%s 个分块）", title, len(chunks))
        return updated or doc
    except Exception:
        await document_repo.update_status(doc.id, "failed")
        raise
