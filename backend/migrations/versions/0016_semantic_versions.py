"""语义注册中心（P2 指标/维度版本与口径审批）：维度版本表。

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-15

对应 doc/语义层注册中心与指标权限治理方案.md §3.4（状态机 / 版本 / 回滚）：
- 指标版本表 `semantic_metric_versions` 已在 0013 建好（P1 随种子写首版快照）；
- 本迁移补齐 **维度版本表 `semantic_dimension_versions`**，使维度与指标对称地支持
  draft→review→published→deprecated 状态机 + 版本快照 + 回滚；
- 同时给 `semantic_metrics` / `semantic_dimensions` 增加 `approved_by` 列
  （审批人留痕，发布/回滚时回填），便于审计口径「谁审批了哪个版本」。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
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


def _column_exists(table: str, col: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(
            c.get("name") == col for c in insp.get_columns(table)
        )
    except Exception:  # noqa: BLE001
        return False


def _index_exists(table: str, name: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(ix.get("name") == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    if not _table_exists("semantic_dimension_versions"):
        op.create_table(
            "semantic_dimension_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("dimension_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            # create | update | publish | deprecate | rollback | submit
            sa.Column("change_type", sa.String(length=16), nullable=False, server_default="create"),
            sa.Column("before_json", sa.JSON(), nullable=True),
            sa.Column("after_json", sa.JSON(), nullable=True),
            sa.Column("operator", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        if not _index_exists("semantic_dimension_versions", "ix_sem_dim_versions_dimension_id"):
            op.create_index(
                "ix_sem_dim_versions_dimension_id",
                "semantic_dimension_versions",
                ["dimension_id"],
            )

    # 审批人留痕：指标 / 维度表各加 approved_by（无 server_default 时 MySQL 要求显式可空）
    if not _column_exists("semantic_metrics", "approved_by"):
        op.add_column(
            "semantic_metrics",
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
        )
    if not _column_exists("semantic_dimensions", "approved_by"):
        op.add_column(
            "semantic_dimensions",
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
        )
    # 指标**版本表**同样需要审批人留痕（发布 / 回滚版本快照回填 approved_by；
    # ORM SemanticMetricVersionModel 含此列，0013 建表时未加，这里补齐）。
    if (
        _table_exists("semantic_metric_versions")
        and not _column_exists("semantic_metric_versions", "approved_by")
    ):
        op.add_column(
            "semantic_metric_versions",
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _table_exists("semantic_metric_versions") and _column_exists(
        "semantic_metric_versions", "approved_by"
    ):
        op.drop_column("semantic_metric_versions", "approved_by")
    if _column_exists("semantic_dimensions", "approved_by"):
        op.drop_column("semantic_dimensions", "approved_by")
    if _column_exists("semantic_metrics", "approved_by"):
        op.drop_column("semantic_metrics", "approved_by")
    if _index_exists("semantic_dimension_versions", "ix_sem_dim_versions_dimension_id"):
        op.drop_index(
            "ix_sem_dim_versions_dimension_id",
            table_name="semantic_dimension_versions",
        )
    if _table_exists("semantic_dimension_versions"):
        op.drop_table("semantic_dimension_versions")
