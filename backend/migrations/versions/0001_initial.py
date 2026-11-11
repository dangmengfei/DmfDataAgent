"""initial schema: sessions/messages/knowledge/cant/metric/trace

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-11

与 backend/dmf_agent/db/models.py 中 Base.metadata 完全对齐。
执行：在 backend/ 目录下 `alembic upgrade head`

注意：
- JSON 列（node_trace / dimensions / skills / trace）按 ORM 定义为 NOT NULL，
  默认值由应用层（Python）写入，故本迁移不设 server_default。
- updated_at 同时带 onupdate（应用层），迁移仅设 server_default，符合模型。
- MySQL 外键默认引擎为 InnoDB + utf8mb4（由库/连接串保证）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default="新会话"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
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
        sa.Index("ix_sessions_user_id", "user_id"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("node_trace", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_messages_session_id", "session_id"),
        sa.Index("ix_messages_created_at", "created_at"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("source_uri", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.Index("ix_knowledge_documents_title", "title"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("vector_ref", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_knowledge_chunks_document_id", "document_id"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "memory_summaries",
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("upto_message_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "metric_definitions",
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=False, server_default=""),
        sa.Column("dimensions", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("code"),
        sa.Index("ix_metric_definitions_name", "name"),
        sa.Index("ix_metric_definitions_category", "category"),
    )

    op.create_table(
        "cant_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("cant_type", sa.String(32), nullable=False),
        sa.Column("cn_name", sa.String(255), nullable=False),
        sa.Column("cant_cn_name", sa.String(255), nullable=False),
        sa.Column("dim_value", sa.String(1024), nullable=False, server_default=""),
        sa.Column("domain", sa.String(16), nullable=False, server_default="dc"),
        sa.Column("status", sa.Integer(), nullable=False, server_default="1"),
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
        sa.Index("ix_cant_entries_tenant_id", "tenant_id"),
    )

    op.create_table(
        "trace_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("mock_mode", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("elapsed_ms", sa.Float(), nullable=True),
        sa.Column("has_error", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("round_id"),
        sa.Index("ix_trace_records_session_id", "session_id"),
        sa.Index("ix_trace_records_intent", "intent"),
        sa.Index("ix_trace_records_has_error", "has_error"),
        sa.Index("ix_trace_records_created_at", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("trace_records")
    op.drop_table("cant_entries")
    op.drop_table("metric_definitions")
    op.drop_table("memory_summaries")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("messages")
    op.drop_table("sessions")
