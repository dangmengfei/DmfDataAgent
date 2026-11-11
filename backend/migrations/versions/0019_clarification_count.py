"""反问澄清次数上限（FR4）：conversation_states 增加 clarification_count 列。

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-17

对应 FR4「单轮最多澄清 N 次」：追问节点读取 prior 澄清次数以判断是否达上限，
达到上限后优雅终止追问（避免循环确认）。计数由 ChatService.snapshot_from 维护
（澄清序列内每追问一次 +1，成功解决后清零）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
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


def upgrade() -> None:
    if not _column_exists("conversation_states", "clarification_count"):
        op.add_column(
            "conversation_states",
            sa.Column("clarification_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _column_exists("conversation_states", "clarification_count"):
        op.drop_column("conversation_states", "clarification_count")
