"""trace_spans 表（OpenTelemetry span 落库）

Revision ID: 0004_trace_spans
Revises: 0003_plugins_prompts
Create Date: 2026-09-12

与 models.py::TraceSpanModel 对齐（纯增量新增表，不改动既有表结构）：

- 观测改为遵循 OpenTelemetry：一轮对话 = root span `chat_round`，
  每个图节点 = 一条子 span `node.<name>`，trace_id / span_id / parent_span_id
  由 OTel SDK 生成；
- span 直接落本表，**不再导出到 agent-insight / 任何 OTLP collector**；
- 轮级汇总仍存 trace_records（既有表结构不变，OTel 元信息在 trace JSON 的 otel 字段）。

注意：MySQL 的 JSON 列不允许字面量 server_default（错误 1101），
故 attributes / events 仅设 nullable=False，默认值由 ORM 侧提供。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_trace_spans"
down_revision: Union[str, None] = "0003_plugins_prompts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="INTERNAL"),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Double(), nullable=True),
        sa.Column("status_code", sa.String(16), nullable=False, server_default="UNSET"),
        sa.Column("status_message", sa.String(512), nullable=False, server_default=""),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("service_name", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_trace_spans_trace_id", "trace_id"),
        sa.Index("ix_trace_spans_span_id", "span_id"),
        sa.Index("ix_trace_spans_session_id", "session_id"),
        sa.Index("ix_trace_spans_round_id", "round_id"),
        sa.Index("ix_trace_spans_name", "name"),
        sa.Index("ix_trace_spans_status_code", "status_code"),
        sa.Index("ix_trace_spans_created_at", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("trace_spans")
