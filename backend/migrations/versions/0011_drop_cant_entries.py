"""清理旧 cant_entries 表

Revision ID: 0011_drop_cant_entries
Revises: 0010_slang_dictionary
Create Date: 2026-09-14

背景：0010 把旧 `cant_entries`（老 aigc_cant_name，仅 dim / dim_value 两类）
搬进新 `slang_entries`，并**保留旧表只读**。实际校验发现 0010 的
`INSERT ... SELECT ... WHERE NOT EXISTS` 未能把全部 3 行旧数据迁入
（与种子词条无重叠却未写入），导致旧表数据游离。

本迁移：
1. **先补齐**：用幂等 `INSERT ... SELECT ... WHERE NOT EXISTS` 把仍缺失的
   旧 cant 行迁入 `slang_entries`（industry 级、builtin、published）；
2. **再删除**旧表 `cant_entries`（守卫 `_table_exists`，幂等）。
3. 旧表相关的 ORM 模型 / 仓储（`CantEntryModel` / `SqlCantRepository` /
   `InMemoryCantRepository` / `CantRepository` Protocol）由代码清理同步移除，
   但本迁移只动表结构，保持单一职责。

执行：backend/ 下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_drop_cant_entries"
down_revision: Union[str, None] = "0010_slang_dictionary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001 - 离线渲染无连接，按"不存在"处理照常 emit DDL
        return None


def _table_exists(table: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        return table in insp.get_table_names()
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    # ---- 1. 补齐：把仍缺失的旧 cant 行迁入 slang_entries（幂等） ----
    if _table_exists("cant_entries") and _table_exists("slang_entries"):
        op.execute(
            sa.text(
                """
                INSERT INTO slang_entries
                    (scope, tenant_id, user_id, term, standard, entry_type, dim_value,
                     rewrite_mode, priority, source, status, version, confidence,
                     hit_count, remark, created_by, updated_by)
                SELECT
                    CASE WHEN c.tenant_id = '' THEN 'industry' ELSE 'tenant' END,
                    c.tenant_id,
                    '',
                    c.cant_cn_name,
                    c.cn_name,
                    CASE WHEN c.cant_type = 'dim_value' THEN 'dim_value' ELSE 'dimension' END,
                    COALESCE(c.dim_value, ''),
                    'expand',
                    100,
                    'builtin',
                    CASE WHEN c.status = 1 THEN 'published' ELSE 'disabled' END,
                    1, 1.0, 0,
                    '由 cant_entries 迁移补全（0011）',
                    'migration',
                    'migration'
                FROM cant_entries c
                WHERE NOT EXISTS (
                    SELECT 1 FROM slang_entries s
                    WHERE s.scope = CASE WHEN c.tenant_id = '' THEN 'industry' ELSE 'tenant' END
                      AND s.tenant_id = c.tenant_id
                      AND s.user_id = ''
                      AND s.entry_type = CASE
                            WHEN c.cant_type = 'dim_value' THEN 'dim_value' ELSE 'dimension' END
                      AND s.term = c.cant_cn_name
                )
                """
            )
        )

    # ---- 2. 删除旧表 ----
    if _table_exists("cant_entries"):
        op.drop_table("cant_entries")


def downgrade() -> None:
    # 反操作为重建旧表结构（数据无法还原，仅恢复壳）
    if not _table_exists("cant_entries"):
        op.create_table(
            "cant_entries",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("cant_type", sa.String(32), nullable=False),
            sa.Column("cn_name", sa.String(255), nullable=False),
            sa.Column("cant_cn_name", sa.String(255), nullable=False),
            sa.Column("dim_value", sa.String(1024), nullable=False, server_default=sa.text("''")),
            sa.Column("domain", sa.String(16), nullable=False, server_default=sa.text("'dc'")),
            sa.Column("status", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            mysql_charset="utf8mb4",
        )
