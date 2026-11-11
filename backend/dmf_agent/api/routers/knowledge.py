"""知识库与知识文档管理接口。

知识库（knowledge_bases）区分 个人 / 企业：
- personal  : owner_id 归属某用户
- enterprise: 企业共享（visibility=public）

文档（knowledge_documents）通过 knowledge_base_id 归属到某个知识库；
不传 knowledge_base_id 的文档视为"未归类"，但**同样带租户**，
因此不会因"没挂知识库"而对其他租户可见（文档级租户隔离）。

租户隔离口径：
- 鉴权开启（DMF_AUTH-ENABLED=true）：列表 / 详情 / 删除 / 检索一律限定当前用户租户；
  跨租户直取按 404 处理（不泄露"存在性"）。
- 鉴权关闭（测试 / 本地）：不做租户过滤，保持既有行为。

对应老系统 sa_knowledge_server.py。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from dmf_agent.api.deps import get_runtime
from dmf_agent.auth.dependencies import get_current_user, require_permission, require_role
from dmf_agent.auth.roles import ADMIN, PERM_KNOWLEDGE, PERM_KNOWLEDGE_WRITE
from dmf_agent.llm.providers import EmbeddingUnavailableError
from dmf_agent.rag.dictionary import DICTIONARY_TITLE, seed_dictionary
from dmf_agent.rag.ingestion import ingest_text, load_file_text
from dmf_agent.rag.vector_store import build_vector_store
from dmf_agent.schemas.auth import CurrentUser
from dmf_agent.schemas.knowledge import (
    ChunkData,
    DictionarySeedRequest,
    DocumentCreate,
    DocumentData,
    KnowledgeBaseCreate,
    KnowledgeBaseData,
    KnowledgeQuery,
)
from dmf_agent.services.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter()


def _guard(permission: str) -> list:
    """路由级权限点依赖（读 / 写两档；admin 通配、鉴权关闭时合成 admin 均放行）。"""
    return [Depends(require_permission(permission))]


def _tenant_scope(current: CurrentUser, runtime: AppRuntime) -> Optional[str]:
    """当前租户过滤值：鉴权开启返回租户 id，关闭返回 None（不过滤）。"""
    return current.tenant_id if runtime.settings.auth.enabled else None


async def _resolve_doc_owner(
    current: CurrentUser, runtime: AppRuntime, knowledge_base_id: Optional[str]
) -> str:
    """计算文档归属用户（检索隔离用）。

    - 鉴权关闭：返回 ""（不隔离）。
    - 倒入企业知识库 / 公开库：返回 ""（同租户全员可见）。
    - 个人/私有库 或 未归类文档：返回上传者 id（仅本人可见）。
    """
    if not runtime.settings.auth.enabled:
        return ""
    if not knowledge_base_id:
        return current.id  # 未归类文档按个人处理，仅本人可见
    # 直接读 KB 行（get 为异步）
    kb = await runtime.db.knowledge_base_repo.get(knowledge_base_id)
    if kb is None:
        return current.id
    if kb.type == "enterprise" or kb.visibility == "public":
        return ""
    return current.id


async def _visible_base(base_id: str, tenant_id: Optional[str], runtime: AppRuntime) -> KnowledgeBaseData:
    """取知识库并做可见性校验；不存在或跨租户统一 404（不泄露存在性）。"""
    base = await runtime.db.knowledge_base_repo.get(base_id)
    if base is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if tenant_id is not None and (base.tenant_id or "") != tenant_id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return base


async def _assert_base_owned(base_id: str, tenant_id: Optional[str], runtime: AppRuntime) -> None:
    """校验文档要挂到的知识库存在且属于当前租户（防止跨租户挂载）。"""
    await _visible_base(base_id, tenant_id, runtime)


# ---------- 知识库（个人 / 企业）----------


@router.get(
    "/knowledge/bases",
    response_model=List[KnowledgeBaseData],
    summary="知识库列表（按租户隔离）",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def list_bases(
    type: Optional[str] = None,
    keyword: Optional[str] = None,
    owner_id: Optional[str] = None,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[KnowledgeBaseData]:
    tenant_id = _tenant_scope(current, runtime)
    bases = await runtime.db.knowledge_base_repo.list(
        type=type, keyword=keyword, owner_id=owner_id, tenant_id=tenant_id
    )
    # 填充每个知识库的文档数
    for b in bases:
        b.doc_count = await runtime.db.document_repo.count_documents(b.id)
    return bases


@router.post(
    "/knowledge/bases",
    response_model=KnowledgeBaseData,
    summary="新建知识库",
    dependencies=_guard(PERM_KNOWLEDGE_WRITE),
)
async def create_base(
    body: KnowledgeBaseCreate,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> KnowledgeBaseData:
    auth_on = runtime.settings.auth.enabled
    owner_id = current.id if (auth_on and body.type == "personal") else body.owner_id
    tenant_id = current.tenant_id if auth_on else ""
    return await runtime.db.knowledge_base_repo.add(
        KnowledgeBaseData(
            id="",
            name=body.name,
            description=body.description,
            type=body.type,
            owner_id=owner_id,
            tenant_id=tenant_id,
            visibility=body.visibility,
        )
    )


@router.get(
    "/knowledge/bases/{base_id}",
    response_model=KnowledgeBaseData,
    summary="知识库详情",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def get_base(
    base_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> KnowledgeBaseData:
    base = await _visible_base(base_id, _tenant_scope(current, runtime), runtime)
    base.doc_count = await runtime.db.document_repo.count_documents(base_id)
    return base


@router.delete(
    "/knowledge/bases/{base_id}",
    summary="删除知识库（级联删文档/分块/向量）",
    dependencies=_guard(PERM_KNOWLEDGE_WRITE),
)
async def delete_base(
    base_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    await _visible_base(base_id, _tenant_scope(current, runtime), runtime)
    # 级联删除该库下所有文档
    docs = await runtime.db.document_repo.list(knowledge_base_id=base_id)
    store = _get_vector_store(runtime) if runtime.retriever is not None else None
    for doc in docs:
        chunks = await runtime.db.chunk_repo.list_by_document(doc.id)
        if store is not None:
            try:
                store.delete([c.vector_ref for c in chunks if c.vector_ref])
            except Exception as exc:  # noqa: BLE001
                logger.warning("向量删除失败：%s", exc)
        await runtime.db.chunk_repo.delete_by_document(doc.id)
        await runtime.db.document_repo.delete(doc.id)
    deleted = await runtime.db.knowledge_base_repo.delete(base_id)
    return {"deleted": deleted, "documents_removed": len(docs)}


# ---------- 知识文档 ----------


@router.get(
    "/knowledge/documents",
    response_model=List[DocumentData],
    summary="文档列表（按租户隔离，含未归类文档）",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def list_documents(
    keyword: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[DocumentData]:
    return await runtime.db.document_repo.list(
        keyword=keyword,
        knowledge_base_id=knowledge_base_id,
        tenant_id=_tenant_scope(current, runtime),
    )


@router.post(
    "/knowledge/documents",
    response_model=DocumentData,
    summary="新增文档（文本）",
    dependencies=_guard(PERM_KNOWLEDGE_WRITE),
)
async def add_document(
    body: DocumentCreate,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> DocumentData:
    if runtime.retriever is None:
        raise HTTPException(
            status_code=400,
            detail="RAG 未配置（本地模式需安装 sentence-transformers 并配置 DMF_LLM-EMBEDDING_BACKEND=local；或配置 DMF_LLM-EMBEDDING_* 外部端点后重启），当前仅支持文档元数据管理",
        )
    tenant_id = _tenant_scope(current, runtime)
    if body.knowledge_base_id:
        await _assert_base_owned(body.knowledge_base_id, tenant_id, runtime)
    owner_id = await _resolve_doc_owner(current, runtime, body.knowledge_base_id)
    try:
        doc = await ingest_text(
            title=body.title,
            text=body.text,
            document_repo=runtime.db.document_repo,
            chunk_repo=runtime.db.chunk_repo,
            vector_store=_get_vector_store(runtime),
            settings=runtime.settings,
            knowledge_base_id=body.knowledge_base_id,
            tenant_id=tenant_id or "",
            owner_id=owner_id,
            authority_level=body.authority_level,
        )
    except EmbeddingUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"向量化服务暂不可用，文档暂未入库：{exc}")
    return doc


@router.post(
    "/knowledge/documents/upload",
    response_model=DocumentData,
    summary="上传文档（txt/md/pdf）",
    dependencies=_guard(PERM_KNOWLEDGE_WRITE),
)
async def upload_document(
    file: UploadFile,
    knowledge_base_id: Optional[str] = Form(None),
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> DocumentData:
    if runtime.retriever is None:
        raise HTTPException(status_code=400, detail="RAG 未配置（本地模式需安装 sentence-transformers 并配置 DMF_LLM-EMBEDDING_BACKEND=local；或配置 DMF_LLM-EMBEDDING_* 外部端点后重启）")
    suffix = Path(file.filename or "upload.txt").suffix.lower()
    if suffix not in (".txt", ".md", ".markdown", ".pdf"):
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix}")

    tenant_id = _tenant_scope(current, runtime)
    if knowledge_base_id:
        await _assert_base_owned(knowledge_base_id, tenant_id, runtime)
    owner_id = await _resolve_doc_owner(current, runtime, knowledge_base_id)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        try:
            text, source_type = load_file_text(tmp_path)
        except RuntimeError as exc:  # 依赖缺失（如未装 loaders extra）
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # 扫描件无文本等可预期的解析失败
            raise HTTPException(status_code=400, detail=f"文档解析失败：{exc}") from exc
        except Exception as exc:  # 损坏文件 / 编码异常等：给可读错误，不返回裸 500
            logger.warning("文档解析失败：%s", exc, exc_info=True)
            raise HTTPException(status_code=400, detail=f"文档解析失败：{exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    if not text.strip():
        raise HTTPException(status_code=400, detail="未从文档中解析出有效文本内容")

    doc = await ingest_text(
        title=Path(file.filename or "未命名").stem,
        text=text,
        document_repo=runtime.db.document_repo,
        chunk_repo=runtime.db.chunk_repo,
        vector_store=_get_vector_store(runtime),
        settings=runtime.settings,
        source_type=source_type,
        source_uri=file.filename,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id or "",
        owner_id=owner_id,
    )
    return doc


@router.get(
    "/knowledge/documents/{doc_id}/chunks",
    response_model=List[ChunkData],
    summary="文档分块列表（核验切分/入库质量，只读）",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def list_document_chunks(
    doc_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> List[ChunkData]:
    """查看一个文档被切分后的全部分块（按 chunk_index 升序）。

    用途：上传后核验切分是否合理、解析内容是否正确（RAG 效果排障：
    检索不准时区分「切分问题」还是「召回/精排问题」）。只读接口，与
    删除接口同款租户校验（跨租户按 404，不泄露存在性）。
    """
    tenant_id = _tenant_scope(current, runtime)
    doc = await runtime.db.document_repo.get(doc_id)
    if doc is None or (tenant_id is not None and (doc.tenant_id or "") != tenant_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    chunks = await runtime.db.chunk_repo.list_by_document(doc_id)
    return sorted(chunks, key=lambda c: c.chunk_index)


@router.delete(
    "/knowledge/documents/{doc_id}",
    summary="删除文档（含分块与向量）",
    dependencies=_guard(PERM_KNOWLEDGE_WRITE),
)
async def delete_document(
    doc_id: str,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    # 跨租户删除按 404（不泄露存在性）
    tenant_id = _tenant_scope(current, runtime)
    doc = await runtime.db.document_repo.get(doc_id)
    if doc is None or (tenant_id is not None and (doc.tenant_id or "") != tenant_id):
        raise HTTPException(status_code=404, detail="文档不存在")

    chunks = await runtime.db.chunk_repo.list_by_document(doc_id)
    if runtime.retriever is not None and chunks:
        try:
            store = _get_vector_store(runtime)
            if store is not None:
                store.delete([c.vector_ref for c in chunks if c.vector_ref])
        except Exception as exc:  # noqa: BLE001 - 向量删除失败不阻断元数据删除
            logger.warning("向量删除失败：%s", exc)
    await runtime.db.chunk_repo.delete_by_document(doc_id)
    deleted = await runtime.db.document_repo.delete(doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"deleted": True}


@router.post(
    "/knowledge/query",
    summary="知识检索（调试，按租户隔离）",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def query_knowledge(
    body: KnowledgeQuery,
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    if runtime.retriever is None:
        raise HTTPException(status_code=400, detail="RAG 未配置")
    tenant_id = _tenant_scope(current, runtime)
    owner_id = current.id if runtime.settings.auth.enabled else None
    docs = await runtime.retriever.retrieve(
        body.query, k=body.top_k, tenant_id=tenant_id, owner_id=owner_id, rerank=body.rerank
    )
    return {"query": body.query, "results": docs}


@router.post(
    "/knowledge/seed/dictionary",
    summary="灌入指标口径字典（权威源，幂等覆盖；仅管理员）",
    dependencies=[Depends(require_role(ADMIN))],
)
async def seed_metric_dictionary(
    body: DictionarySeedRequest,
    current: CurrentUser = Depends(require_role(ADMIN)),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    """把语义层的指标口径渲染成知识库文档，作为**权威源**灌入（FR7）。

    - 内容由 `semantic/manifest.yml` 确定性生成，不接收外部文本；
    - 幂等：同一 `source_uri` 的旧文档会被覆盖更新，重复调用不会产生重复文档；
    - 打分档 `authoritative`，检索时按 `DMF_RAG-AUTHORITY_WEIGHTS` 加权优先返回。
    """
    if runtime.semantic_layer is None:
        raise HTTPException(status_code=503, detail="语义层未装配，无法生成口径字典")

    tenant_id = _tenant_scope(current, runtime)
    if body.knowledge_base_id:
        await _assert_base_owned(body.knowledge_base_id, tenant_id, runtime)
    try:
        result = await seed_dictionary(
            document_repo=runtime.db.document_repo,
            chunk_repo=runtime.db.chunk_repo,
            vector_store=_get_vector_store(runtime),
            settings=runtime.settings,
            layer=runtime.semantic_layer,
            tenant_id=tenant_id or "",
            knowledge_base_id=body.knowledge_base_id,
        )
    except EmbeddingUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"向量化服务暂不可用，口径字典未灌入：{exc}")
    return {**result, "authority_counts": await runtime.db.document_repo.count_by_authority()}


@router.get(
    "/knowledge/authority/stats",
    summary="知识库来源分级统计（权威源 / 沉淀源 / 用户上传）",
    dependencies=_guard(PERM_KNOWLEDGE),
)
async def authority_stats(
    current: CurrentUser = Depends(get_current_user),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict:
    counts = await runtime.db.document_repo.count_by_authority()
    return {
        "counts": counts,
        "weights": runtime.settings.rag.authority_weights,
        "dictionary_title": DICTIONARY_TITLE,
        "rerank_available": runtime.rerank_available,
    }


def _get_vector_store(runtime: AppRuntime):
    """按需重建向量库句柄（进程内 FAISS 单例由文件目录保证一致）。"""
    return build_vector_store(runtime.settings)
