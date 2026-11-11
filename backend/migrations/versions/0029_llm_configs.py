"""模型管理：llm_configs 表（对话 LLM 配置入库，is_default 标记运行时生效模型）。

幂等守卫：表已存在则跳过（与 0022 同款 `_table_exists`）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _table_exists("llm_configs"):
        op.create_table(
            "llm_configs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=True),
            sa.Column("model", sa.String(length=128), server_default="", nullable=True),
            sa.Column("base_url", sa.String(length=512), server_default="", nullable=True),
            sa.Column("api_key", sa.String(length=512), server_default="", nullable=True),
            sa.Column("temperature", sa.Float(), server_default="0.1", nullable=True),
            sa.Column("timeout", sa.Integer(), server_default="120", nullable=True),
            sa.Column("max_retries", sa.Integer(), server_default="2", nullable=True),
            sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=True),
            sa.Column("status", sa.String(length=16), server_default="enabled", nullable=True),
            # TEXT 列 MySQL 不允许 server_default（默认值走 ORM 侧 Python default）
            sa.Column("last_test", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_llm_configs_name", "llm_configs", ["name"], unique=True)
        op.create_index("ix_llm_configs_is_default", "llm_configs", ["is_default"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_configs_is_default", table_name="llm_configs")
    op.drop_index("ix_llm_configs_name", table_name="llm_configs")
    op.drop_table("llm_configs")
