"""多轮对话状态管理（P0-6）：conversation_states 表。

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-15

对应 doc/语义层注册中心与指标权限治理方案.md 之外的 P0-6 专项——
「多轮对话状态管理」：每轮成功查询后落库**已解析实体**（指标 / 维度 / 过滤 /
时间窗 / 主题 / 原始与改写问句 / 挂起澄清 + 状态机），下一轮注入 `context_entities`
供要素抽取与改写节点做**确定性指代消解**（"它 / 这个指标 / 按地区拆分 / 和上个月比"
不再只依赖模糊的 LLM 改写）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001
        return None


def _table_exists(name: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and name in (insp.get_table_names() or [])
    except Exception:  # noqa: BLE001
        return False


def _index_exists(table: str, name: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(ix.get("name") == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    if not _table_exists("conversation_states"):
        op.create_table(
            "conversation_states",
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("intent", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("active_metrics", sa.JSON(), nullable=False),
            sa.Column("active_dimensions", sa.JSON(), nullable=False),
            sa.Column("active_filters", sa.JSON(), nullable=False),
            sa.Column("time_range", sa.JSON(), nullable=False),
            sa.Column("topic", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("last_query", sa.Text(), nullable=False),
            sa.Column("last_query_rewritten", sa.Text(), nullable=False),
            sa.Column("pending_clarification", sa.JSON(), nullable=False),
            sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
            # active | awaiting_clarification | idle（最长 awaiting_clarification=21 字符，
            # 故用 String(32) 预留余量）
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("session_id"),
        )
        if not _index_exists("conversation_states", "ix_conv_state_tenant_id"):
            op.create_index(
                "ix_conv_state_tenant_id", "conversation_states", ["tenant_id"]
            )
        if not _index_exists("conversation_states", "ix_conv_state_user_id"):
            op.create_index(
                "ix_conv_state_user_id", "conversation_states", ["user_id"]
            )


def downgrade() -> None:
    if _index_exists("conversation_states", "ix_conv_state_user_id"):
        op.drop_index("ix_conv_state_user_id", table_name="conversation_states")
    if _index_exists("conversation_states", "ix_conv_state_tenant_id"):
        op.drop_index("ix_conv_state_tenant_id", table_name="conversation_states")
    if _table_exists("conversation_states"):
        op.drop_table("conversation_states")
