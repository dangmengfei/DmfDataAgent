"""局部租户覆盖（overlay）：semantic_published 增加 mode 列。

语义：
- ``full``    —— 整份已合并 manifest（0023 的既有行为，历史行按此回填）；
- ``overlay`` —— 只含被改动的 semantic_models / metrics 条目，运行时按 name
  整条替换叠加到基座（全局 full 快照或 YAML 基座）之上。

幂等：inspector 守卫，列已存在则跳过。VARCHAR 允许 server_default，
故历史行可安全回填为 ``full``（与运行时 ``normalize_mode`` 的默认值一致）。
"""

from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects import mysql  # noqa: F401  （保留以便后续 MySQL 专属类型调整）

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_TABLE = "semantic_published"
_COLUMN = "mode"


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(length=16),
                nullable=False,
                server_default="full",
            ),
        )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
