"""plugins 表（Skill / MCP 注册）+ prompt_versions 表（系统提示词版本）

Revision ID: 0003_plugins_prompts
Revises: 0002_kb_tokens
Create Date: 2026-09-12

与 models.py 对齐（在 0002 基础上增量）：
- 新建 plugins 表：用户自定义 Skill / MCP Server 注册项（内置 Skill 不落库，由代码提供）；
- 新建 prompt_versions 表：系统提示词不可变历史版本，status=published 为当前生效版本。

注意：MySQL 的 TEXT / BLOB / JSON 列不允许字面量 server_default（错误 1101），
故 description / config / system / human 等文本列仅设 nullable=False，默认值由 ORM 侧提供。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_plugins_prompts"
down_revision: Union[str, None] = "0002_kb_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("plugin_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False, server_default=""),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="enabled"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_plugins_plugin_type", "plugin_type"),
        sa.Index("ix_plugins_name", "name"),
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("system", sa.Text(), nullable=False),
        sa.Column("human", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_version", sa.String(16), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_prompt_versions_name", "name"),
        sa.Index("ix_prompt_versions_status", "status"),
        sa.Index("ix_prompt_versions_created_at", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("prompt_versions")
    op.drop_table("plugins")
