"""混合方案（YAML 基座 → DB 发布快照）：semantic_published 表。

语义：
- 一条记录 = 某租户某版本的「已合并 manifest 原始字典」快照（payload）。
- tenant_id=""：全局默认快照（所有租户共享）；同租户内 version 递增。
- state 复用语义注册中心状态机 draft→review→published→deprecated；
  运行时只消费 state=published 的最新版本，无该租户快照时回落全局。
- 幂等：inspector 守卫，已存在表跳过。
"""

from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects import mysql  # noqa: F401  （保留以便后续 MySQL 专属类型调整）

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _table_exists("semantic_published"):
        op.create_table(
            "semantic_published",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=True),
            sa.Column("version", sa.Integer(), nullable=True),
            sa.Column("state", sa.String(length=16), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_semantic_pub_lookup", "semantic_published", ["tenant_id", "state", "version"], unique=False
        )
        op.create_index("ix_semantic_published_state", "semantic_published", ["state"], unique=False)
        op.create_index("ix_semantic_published_tenant_id", "semantic_published", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_table("semantic_published")
