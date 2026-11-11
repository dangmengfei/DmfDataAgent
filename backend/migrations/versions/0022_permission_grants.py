"""FR13 P5/P6 授权存储：metric_grants / data_scopes / dimension_policies（enforce 用）。

语义：
- 空表 = 不限制（指标白名单为空时全部可见；行/列策略为空时无约束）。
- 一旦某 user_id / role 有记录，则该主体据策略受限。
- 幂等：inspector 守卫，已存在表跳过；迁移给的列均为可空 + 默认值，与 ORM 一致。
"""

from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects import mysql  # noqa: F401  （保留以便后续 MySQL 专属类型调整）

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _table_exists("metric_grants"):
        op.create_table(
            "metric_grants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=True),
            sa.Column("user_id", sa.String(length=64), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=True),
            sa.Column("metric_key", sa.String(length=255), nullable=True),
            sa.Column("granted_by", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_metric_grant_lookup", "metric_grants", ["tenant_id", "user_id", "role"], unique=False)
        op.create_index("ix_metric_grants_metric_key", "metric_grants", ["metric_key"], unique=False)

    if not _table_exists("data_scopes"):
        op.create_table(
            "data_scopes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=True),
            sa.Column("user_id", sa.String(length=64), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=True),
            sa.Column("dimension", sa.String(length=128), nullable=True),
            sa.Column("allowed_values", sa.JSON(), nullable=True),
            sa.Column("granted_by", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_data_scope_lookup", "data_scopes", ["tenant_id", "user_id", "role"], unique=False)
        op.create_index("ix_data_scopes_dimension", "data_scopes", ["dimension"], unique=False)

    if not _table_exists("dimension_policies"):
        op.create_table(
            "dimension_policies",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=True),
            sa.Column("user_id", sa.String(length=64), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=True),
            sa.Column("dimension", sa.String(length=128), nullable=True),
            sa.Column("denied", sa.Boolean(), nullable=True),
            sa.Column("granted_by", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dim_policy_lookup", "dimension_policies", ["tenant_id", "user_id", "role"], unique=False)
        op.create_index("ix_dim_policies_dimension", "dimension_policies", ["dimension"], unique=False)


def downgrade() -> None:
    op.drop_table("dimension_policies")
    op.drop_table("data_scopes")
    op.drop_table("metric_grants")
