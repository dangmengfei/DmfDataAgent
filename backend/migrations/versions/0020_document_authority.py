"""知识来源分级（FR7）：knowledge_documents 增加 authority_level 列。

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-17

对应 FR7「知识库分级」：
- authoritative 权威源：口径文档 / 指标字典，由系统确定性同步（可幂等重灌）
- sediment      沉淀源：对话点赞后人工审核入库
- user          用户上传（默认值，历史数据全部落到该档）

检索链路按该档位加权（权威源 > 沉淀源 > 用户上传），见 `rag/retriever.py`。
加列带 server_default 且可空性为 NOT NULL，历史行自动回填 'user'，无破坏性变更。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001
        return None


def _column_exists(table: str, column: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(
            c.get("name") == column for c in insp.get_columns(table)
        )
    except Exception:  # noqa: BLE001
        return False


def _index_exists(table: str, index: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(
            i.get("name") == index for i in insp.get_indexes(table)
        )
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    if not _column_exists("knowledge_documents", "authority_level"):
        op.add_column(
            "knowledge_documents",
            sa.Column(
                "authority_level", sa.String(length=16), nullable=False, server_default="user"
            ),
        )
    if not _index_exists("knowledge_documents", "ix_knowledge_documents_authority_level"):
        op.create_index(
            "ix_knowledge_documents_authority_level",
            "knowledge_documents",
            ["authority_level"],
        )


def downgrade() -> None:
    if _index_exists("knowledge_documents", "ix_knowledge_documents_authority_level"):
        op.drop_index(
            "ix_knowledge_documents_authority_level", table_name="knowledge_documents"
        )
    if _column_exists("knowledge_documents", "authority_level"):
        op.drop_column("knowledge_documents", "authority_level")
