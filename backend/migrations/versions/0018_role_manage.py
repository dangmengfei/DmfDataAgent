"""角色管理（RBAC 角色 DB 化）：roles 表。

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-16

把原本硬编码在 auth/roles.py 的三级角色落库，并支持自定义角色与权限点调整。
内置角色（admin/analyst/viewer）由应用启动时幂等种子导入（is_builtin=1）；
permissions 为 JSON 数组（admin 为 ["*"]，其余为 ALL_PERMISSIONS 子集）。
TEXT/JSON 列不设 server_default（沿用 0015 起的约定，由 Python 侧默认值兜底）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
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
    if not _table_exists("roles"):
        op.create_table(
            "roles",
            sa.Column("name", sa.String(length=32), nullable=False),
            sa.Column("label", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("permissions", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("name"),
        )
        if not _index_exists("roles", "ix_roles_status"):
            op.create_index("ix_roles_status", "roles", ["status"])


def downgrade() -> None:
    if _index_exists("roles", "ix_roles_status"):
        op.drop_index("ix_roles_status", table_name="roles")
    if _table_exists("roles"):
        op.drop_table("roles")
