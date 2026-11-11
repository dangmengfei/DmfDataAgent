"""knowledge_bases 表 + 文档归属 + trace token 统计

Revision ID: 0002_kb_tokens
Revises: 0001_initial
Create Date: 2026-09-11

与 models.py 对齐（在 0001_initial 基础上增量变更）：
- 新建 knowledge_bases 表（个人 / 企业知识库）
- knowledge_documents 增加 knowledge_base_id（可空，归属知识库）
- trace_records 增加 prompt_tokens / completion_tokens / total_tokens（可空）

执行：在 backend/ 目录下 `alembic upgrade head`
注意：所有新增列均 nullable，无需 server_default（MySQL TEXT/JSON 也不允许字面量默认值）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_kb_tokens"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("type", sa.String(16), nullable=False, server_default="personal"),
        sa.Column("owner_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_knowledge_bases_name", "name"),
        sa.Index("ix_knowledge_bases_type", "type"),
    )

    op.add_column(
        "knowledge_documents",
        sa.Column("knowledge_base_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_knowledge_documents_knowledge_base_id",
        "knowledge_documents",
        ["knowledge_base_id"],
    )

    op.add_column(
        "trace_records", sa.Column("prompt_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "trace_records", sa.Column("completion_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "trace_records", sa.Column("total_tokens", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_trace_records_total_tokens", "trace_records", ["total_tokens"]
    )


def downgrade() -> None:
    op.drop_index("ix_trace_records_total_tokens", table_name="trace_records")
    op.drop_column("trace_records", "total_tokens")
    op.drop_column("trace_records", "completion_tokens")
    op.drop_column("trace_records", "prompt_tokens")

    op.drop_index(
        "ix_knowledge_documents_knowledge_base_id", table_name="knowledge_documents"
    )
    op.drop_column("knowledge_documents", "knowledge_base_id")

    op.drop_table("knowledge_bases")
