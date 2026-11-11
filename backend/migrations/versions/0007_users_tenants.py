"""用户 / 租户 / 角色（登录、多租户隔离）

Revision ID: 0007_users_tenants
Revises: 0006_memory_l1_l3
Create Date: 2026-09-13

对齐 models.py：

1. 新建 tenants（租户）：id / code(唯一) / name / status / created_at
2. 新建 users（用户）：id / username(唯一) / password_hash / display_name /
   tenant_id / role / status / created_at / updated_at / last_login_at
   —— 口令以 PBKDF2-HMAC-SHA256 哈希存储（pbkdf2_sha256$iter$salt$hash），不存明文。
3. sessions / knowledge_bases 增加 tenant_id（多租户隔离：按租户过滤）。

幂等：列 / 表已存在则跳过（迁移中途失败后重跑不报 Duplicate / already exists）。
首次启动无用户时由应用引导默认租户(default) + 管理员(admin)，见 auth.service.AuthService。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_users_tenants"
down_revision: Union[str, None] = "0006_memory_l1_l3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001 - 离线渲染无连接，按"不存在"处理照常 emit DDL
        return None


def _table_exists(name: str) -> bool:
    insp = _inspector()
    return bool(insp) and name in insp.get_table_names()


def _column_exists(table: str, name: str) -> bool:
    insp = _inspector()
    return bool(insp) and any(c["name"] == name for c in insp.get_columns(table))


def upgrade() -> None:
    # 1) 租户
    if not _table_exists("tenants"):
        op.create_table(
            "tenants",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code", name="uq_tenants_code"),
            sa.Index("ix_tenants_code", "code"),
            sa.Index("ix_tenants_status", "status"),
        )

    # 2) 用户
    if not _table_exists("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("username", sa.String(64), nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(128), nullable=False, server_default=sa.text("''")),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("role", sa.String(16), nullable=False, server_default=sa.text("'viewer'")),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            # onupdate 由 ORM 维护（对齐 models.UserModel.onupdate=func.now()），DDL 仅设默认值
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username", name="uq_users_username"),
            sa.Index("ix_users_username", "username"),
            sa.Index("ix_users_tenant_id", "tenant_id"),
            sa.Index("ix_users_role", "role"),
            sa.Index("ix_users_status", "status"),
        )

    # 3) 会话 / 知识库加租户列（可空默认空串，既有数据不受影响）
    if not _column_exists("sessions", "tenant_id"):
        op.add_column(
            "sessions",
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
        )
        op.create_index("ix_sessions_tenant_id", "sessions", ["tenant_id"])
    if not _column_exists("knowledge_bases", "tenant_id"):
        op.add_column(
            "knowledge_bases",
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
        )
        op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])


def downgrade() -> None:
    if _column_exists("knowledge_bases", "tenant_id"):
        try:
            op.drop_index("ix_knowledge_bases_tenant_id", table_name="knowledge_bases")
        except Exception:  # noqa: BLE001
            pass
        op.drop_column("knowledge_bases", "tenant_id")
    if _column_exists("sessions", "tenant_id"):
        try:
            op.drop_index("ix_sessions_tenant_id", table_name="sessions")
        except Exception:  # noqa: BLE001
            pass
        op.drop_column("sessions", "tenant_id")
    if _table_exists("users"):
        op.drop_table("users")
    if _table_exists("tenants"):
        op.drop_table("tenants")
