"""改写埋点表（命中率 / 误改写率看板数据源）

Revision ID: 0012_slang_rewrite_events
Revises: 0011_drop_cant_entries
Create Date: 2026-09-14

每次查询改写节点执行后 best-effort 落一条事件：
- `hit_count` / `is_rewritten` -> 命中率；
- `feedback`（1=正确 / -1=误改写 / NULL=未评价）-> 误改写率（前端改写徽标回写）。

`query_hash` = 归一化问句哈希，作为"同一问句"反馈去重键。

执行：backend/ 下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_slang_rewrite_events"
down_revision: Union[str, None] = "0011_drop_cant_entries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001
        return None


def _table_exists(table: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001
        return False


def _index_exists(table: str, name: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        return any(ix["name"] == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    if not _table_exists("slang_rewrite_events"):
        op.create_table(
            "slang_rewrite_events",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("user_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("session_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("message_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("query_hash", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("query_text", sa.String(512), nullable=False, server_default=sa.text("''")),
            sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_rewritten", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("feedback", sa.SmallInteger(), nullable=True),
            sa.Column("note", sa.String(255), nullable=False, server_default=sa.text("''")),
            sa.PrimaryKeyConstraint("id"),
            mysql_charset="utf8mb4",
        )
    for ix, col in (
        ("ix_slang_rewrite_events_created_at", "created_at"),
        ("ix_slang_rewrite_events_tenant_id", "tenant_id"),
        ("ix_slang_rewrite_events_user_id", "user_id"),
        ("ix_slang_rewrite_events_query_hash", "query_hash"),
        ("ix_slang_rewrite_events_feedback", "feedback"),
    ):
        if not _index_exists("slang_rewrite_events", ix):
            op.create_index(ix, "slang_rewrite_events", [col])


def downgrade() -> None:
    if _table_exists("slang_rewrite_events"):
        op.drop_table("slang_rewrite_events")
