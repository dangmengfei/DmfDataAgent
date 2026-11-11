"""L2 用户偏好 + 文档级租户隔离

Revision ID: 0008_preferences_doc_tenant
Revises: 0007_users_tenants
Create Date: 2026-09-14

对齐 models.py：

1. 新建 `user_preferences`（L2 用户偏好，个人维度长期记忆）：
   id / user_id / tenant_id / pref_key / value / source / confidence / status /
   created_at / updated_at，唯一约束 (user_id, pref_key)。
   —— 列名用 `pref_key` 而非 `key`：`key` 是 MySQL 保留字，避免各层反复加引号。

2. `knowledge_documents` 增加 `tenant_id` + 索引：
   补齐文档级租户隔离——改造前只有知识库（knowledge_bases）带租户，
   **未归类文档**（knowledge_base_id 为空）不带租户，会跨租户可见/可检索。

幂等：表 / 列已存在则跳过（中途失败后重跑不报 Duplicate / already exists）。
存量数据处理：新增列默认空串（不改变既有可见性，鉴权关闭时行为完全一致）；
如需把存量未归类文档归入某租户，可在迁移后手工 UPDATE。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_preferences_doc_tenant"
down_revision: Union[str, None] = "0007_users_tenants"
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
    # 1) L2 用户偏好
    if not _table_exists("user_preferences"):
        op.create_table(
            "user_preferences",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("pref_key", sa.String(64), nullable=False),
            # value 为 TEXT：MySQL 禁止 TEXT 列带字面量默认值（1101），默认值由 ORM 提供
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'user'")),
            sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            # onupdate 由 ORM 维护（对齐 models.UserPreferenceModel.onupdate=func.now()）
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "pref_key", name="uq_user_preferences_user_key"),
            sa.Index("ix_user_preferences_user_id", "user_id"),
            sa.Index("ix_user_preferences_tenant_id", "tenant_id"),
            sa.Index("ix_user_preferences_source", "source"),
            sa.Index("ix_user_preferences_status", "status"),
        )

    # 2) 文档级租户隔离
    if not _column_exists("knowledge_documents", "tenant_id"):
        op.add_column(
            "knowledge_documents",
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
        )
        op.create_index(
            "ix_knowledge_documents_tenant_id", "knowledge_documents", ["tenant_id"]
        )


def downgrade() -> None:
    if _column_exists("knowledge_documents", "tenant_id"):
        try:
            op.drop_index("ix_knowledge_documents_tenant_id", table_name="knowledge_documents")
        except Exception:  # noqa: BLE001
            pass
        op.drop_column("knowledge_documents", "tenant_id")
    if _table_exists("user_preferences"):
        op.drop_table("user_preferences")
