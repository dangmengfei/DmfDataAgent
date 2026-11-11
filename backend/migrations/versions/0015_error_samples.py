"""0015 error_samples

错误样本池表（PRD FR6）：报错分类 + 限次自愈样本落库，
供观测/分析自愈成功率与错误分布。

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-14 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "error_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("session_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("user_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("tenant_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("round_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("error_code", sa.String(length=16), server_default="", nullable=False),
        sa.Column("category", sa.Integer(), server_default="0", nullable=False),
        sa.Column("category_label", sa.String(length=16), server_default="", nullable=False),
        sa.Column("node", sa.String(length=48), server_default="", nullable=False),
        sa.Column("message", sa.String(length=512), server_default="", nullable=False),
        # TEXT 列在 MySQL 下不能有 server_default，由应用层 Python default="" 兜底
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("recovered", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("fix_hint", sa.String(length=32), server_default="", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_snippet", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_error_samples_created_at", "error_samples", ["created_at"], unique=False)
    op.create_index("ix_error_samples_session_id", "error_samples", ["session_id"], unique=False)
    op.create_index("ix_error_samples_user_id", "error_samples", ["user_id"], unique=False)
    op.create_index("ix_error_samples_error_code", "error_samples", ["error_code"], unique=False)
    op.create_index("ix_error_samples_category", "error_samples", ["category"], unique=False)
    op.create_index("ix_error_samples_node", "error_samples", ["node"], unique=False)


def downgrade() -> None:
    op.drop_table("error_samples")
