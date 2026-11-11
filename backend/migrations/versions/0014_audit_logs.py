"""0014 audit_logs

合规审计日志表（PRD FR13）：全链路审计事件落库。

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-14 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, None] = "0013_semantic_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), server_default="info", nullable=False),
        sa.Column("session_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("user_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("tenant_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("round_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("summary", sa.String(length=512), server_default="", nullable=False),
        sa.Column("detail_json", mysql.JSON(), nullable=False),
        sa.Column("client_info", sa.String(length=255), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"], unique=False)
    op.create_index("ix_audit_logs_level", "audit_logs", ["level"], unique=False)
    op.create_index("ix_audit_logs_session_id", "audit_logs", ["session_id"], unique=False)
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("audit_logs")


# MySQL JSON 占位
_ = mysql
