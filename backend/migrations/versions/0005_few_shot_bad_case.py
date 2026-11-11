"""few_shot_examples / bad_cases 表（点赞 / 点踩反馈落库）

Revision ID: 0005_few_shot_bad_case
Revises: 0004_trace_spans
Create Date: 2026-09-12

与 models.py::FewShotExampleModel / BadCaseModel 对齐（纯增量新增表，不改动既有表结构）：

- 用户点赞助手回答 -> 写入 few_shot_examples（few-shot 经验库数据源）；
  意图识别阶段按 BM25 召回相似样本，把命中样本的意图期望输出注入 prompt。
- 用户点踩助手回答 -> 写入 bad_cases（bad case 库）；当前仅落库，供后续分析与回流。

注意：MySQL 的 JSON 列不允许字面量 server_default（错误 1101），
但本迁移两表无 JSON 列，故 DateTime 列可用 server_default=now()。

执行：在 backend/ 目录下 `alembic upgrade head`
（本地开发 auto_create_tables=True 已由 ORM metadata.create_all 自动建表，无需手动迁移）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_few_shot_bad_case"
down_revision: Union[str, None] = "0004_trace_spans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "few_shot_examples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("category", sa.String(32), nullable=False, server_default="intention"),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_few_shot_examples_session_id", "session_id"),
        sa.Index("ix_few_shot_examples_message_id", "message_id"),
        sa.Index("ix_few_shot_examples_intent", "intent"),
        sa.Index("ix_few_shot_examples_category", "category"),
        sa.Index("ix_few_shot_examples_created_at", "created_at"),
    )
    op.create_table(
        "bad_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_bad_cases_session_id", "session_id"),
        sa.Index("ix_bad_cases_message_id", "message_id"),
        sa.Index("ix_bad_cases_intent", "intent"),
        sa.Index("ix_bad_cases_created_at", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("bad_cases")
    op.drop_table("few_shot_examples")
