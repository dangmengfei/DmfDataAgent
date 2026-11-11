"""知识文档归属用户（个人/企业隔离）：knowledge_documents 增加 owner_id 列。

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-20

对应「知识库重点做」检索隔离修复：同租户内 A 的个人私有库不应被 B 检索到。
检索时在 tenant_id 过滤基础上，进一步按 owner_id 收窄个人/私有文档可见性。
加列带 server_default='' 且 NOT NULL，历史行回填空串（视为"无主/全员可见"），无破坏性变更。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
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
    if not _column_exists("knowledge_documents", "owner_id"):
        op.add_column(
            "knowledge_documents",
            sa.Column(
                "owner_id", sa.String(length=64), nullable=False, server_default=""
            ),
        )
    if not _index_exists("knowledge_documents", "ix_knowledge_documents_owner_id"):
        op.create_index(
            "ix_knowledge_documents_owner_id",
            "knowledge_documents",
            ["owner_id"],
        )


def downgrade() -> None:
    if _index_exists("knowledge_documents", "ix_knowledge_documents_owner_id"):
        op.drop_index(
            "ix_knowledge_documents_owner_id", table_name="knowledge_documents"
        )
    if _column_exists("knowledge_documents", "owner_id"):
        op.drop_column("knowledge_documents", "owner_id")
