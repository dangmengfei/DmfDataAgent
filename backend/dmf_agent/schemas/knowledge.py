"""知识库文档模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DocumentData(BaseModel):
    id: str
    title: str
    source_type: str = "text"  # text | pdf | md | txt
    source_uri: Optional[str] = None
    status: str = "pending"  # pending | indexed | failed
    chunk_count: int = 0
    knowledge_base_id: Optional[str] = None  # 归属知识库
    # 来源分级（FR7）：authoritative 权威源（口径文档/指标字典，系统同步）
    # | sediment 沉淀源（点赞/人工审核后入库） | user 用户上传（默认）
    # 检索时按该字段加权：权威源 > 沉淀源 > 用户上传。
    authority_level: str = "user"
    # 所属租户（多租户隔离：未归类文档同样带租户，避免"孤儿文档"跨租户可见）
    tenant_id: str = ""
    # 文档归属用户（个人/私有库=上传者，企业/公开库或系统权威源=""）；检索配合 tenant_id 做个人隔离
    owner_id: str = ""
    # 由仓储层填充（内存实现填 now，MySQL 实现取数据库时间戳）
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    knowledge_base_id: Optional[str] = None  # 可选：入库到指定知识库
    # 来源分级：默认 user（用户上传）；权威源/沉淀源由专用入口写入
    authority_level: str = Field("user", pattern="^(authoritative|sediment|user)$")


class KnowledgeQuery(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: Optional[int] = None
    # 检索是否启用重排与分级加权（默认走 settings；显式关闭便于对比调试）
    rerank: Optional[bool] = None


class DictionarySeedRequest(BaseModel):
    """权威源灌入请求：指标口径字典（内容由语义层确定性生成，不接受外部文本）。"""

    # 可选：灌入到指定知识库；为空则作为「未归类」权威文档（同样带租户）
    knowledge_base_id: Optional[str] = None


class ChunkData(BaseModel):
    id: str
    document_id: str
    chunk_index: int
    content: str
    vector_ref: Optional[str] = None  # 向量库中的 chunk id


# ---- 知识库（个人 / 企业）----


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    # personal: 个人知识库；enterprise: 企业共享知识库
    type: str = Field("personal", pattern="^(personal|enterprise)$")
    owner_id: str = ""  # 个人知识库归属用户；企业可空
    # private: 仅 owner 可见；public: 全员可见
    visibility: str = Field("private", pattern="^(private|public)$")


class KnowledgeBaseData(BaseModel):
    id: str
    name: str
    description: str = ""
    type: str = "personal"
    owner_id: str = ""
    tenant_id: str = ""
    visibility: str = "private"
    doc_count: int = 0  # 知识库内文档数（仓储层统计）
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
