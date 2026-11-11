"""指标口径 YAML 定义落库：semantic_metrics 增加 yaml_defn 列。

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-20

对应 FR8 PRD 5.9 闭环：发布指标需生成 manifest 兼容的 yaml_defn 并落库，
供知识库口径字典增量重建与可观测性。

兼容性说明：MySQL 不允许对 JSON / TEXT / BLOB 列设置 DB 级默认值，故本列
`nullable=True` 且不带 `server_default`；新行由 ORM `default=dict` 在写入侧填充 `{}`，
历史行迁移后为 NULL，读取侧统一 `or {}` 兜底，无破坏性变更。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
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
    if not _column_exists("semantic_metrics", "yaml_defn"):
        op.add_column(
            "semantic_metrics",
            sa.Column("yaml_defn", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("semantic_metrics", "yaml_defn"):
        op.drop_column("semantic_metrics", "yaml_defn")
