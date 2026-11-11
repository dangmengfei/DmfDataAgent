"""语义层发布快照审批审计列：semantic_published 增加 approved_by / rejected_by / reject_reason。

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-20

对应 T198d（快照批量 + 驳回原因）：审批流需要记录「谁审批 / 谁驳回 / 驳回原因」，
便于审计与重新提交。三列均为 VARCHAR（NOT NULL + server_default=''）：MySQL 允许
VARCHAR 带默认值（仅 TEXT/JSON/BLOB 不允许），加上默认值可保证既有行在加列后立即可读，
不必回填。ORM 侧同时带 `default=""`，读取侧统一 `or ""` 兜底。

幂等守卫：已存在则跳过 add_column，避免重复迁移报错。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
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
    if not _column_exists("semantic_published", "approved_by"):
        op.add_column(
            "semantic_published",
            sa.Column("approved_by", sa.String(64), nullable=False, server_default=""),
        )
    if not _column_exists("semantic_published", "rejected_by"):
        op.add_column(
            "semantic_published",
            sa.Column("rejected_by", sa.String(64), nullable=False, server_default=""),
        )
    if not _column_exists("semantic_published", "reject_reason"):
        op.add_column(
            "semantic_published",
            sa.Column("reject_reason", sa.String(512), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _column_exists("semantic_published", "reject_reason"):
        op.drop_column("semantic_published", "reject_reason")
    if _column_exists("semantic_published", "rejected_by"):
        op.drop_column("semantic_published", "rejected_by")
    if _column_exists("semantic_published", "approved_by"):
        op.drop_column("semantic_published", "approved_by")
