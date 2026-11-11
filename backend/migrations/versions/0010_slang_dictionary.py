"""黑话词典三级化（行业级 / 公司级 / 用户级）+ 查询改写

Revision ID: 0010_slang_dictionary
Revises: 0009_few_shot_manage
Create Date: 2026-09-14

对齐 models.py::SlangEntryModel / SlangEntryVersionModel，落地 PRD §5.3.1
「查询改写与黑话字典」的数据层：

1. 新建 `slang_entries` —— 三级黑话词典（统一承载 指标 / 维度 / 枚举值 / 术语 / 停用词），
   同层同类型同词唯一（uk_slang），仅 status='published' 参与线上解析。
2. 新建 `slang_entry_versions` —— 词条版本审计（每次变更留前后快照，支持版本对比 / 回滚）。
3. **数据搬迁**：把旧 `cant_entries`（老 aigc_cant_name，仅 dim / dim_value 两类）搬进新表：
       cant_type='dim'       -> entry_type='dimension'
       cant_type='dim_value' -> entry_type='dim_value'（dim_value 列原样带上）
       tenant_id 非空        -> scope='tenant'（公司级），否则 scope='industry'（行业级）
       status=1              -> status='published'，否则 'disabled'
       source='builtin'（视为平台内置）
   搬迁用 `INSERT ... SELECT ... WHERE NOT EXISTS` 守卫，**可重复执行**。
   旧表 `cant_entries` **保留不删**（只读快照，便于回滚对比）。

`trace_records` **不动**：查询改写记录落在既有 `trace` JSON 里（`trace["query_rewrite"]`），
与 `trace["otel"]` 同一策略，避免对既有表做 ALTER。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_slang_dictionary"
down_revision: Union[str, None] = "0009_few_shot_manage"
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


def _index_exists(table: str, name: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        return any(ix["name"] == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    if not _table_exists("slang_entries"):
        op.create_table(
            "slang_entries",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("user_id", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("term", sa.String(128), nullable=False),
            sa.Column("standard", sa.String(255), nullable=False, server_default=sa.text("''")),
            sa.Column("entry_type", sa.String(16), nullable=False, server_default=sa.text("'term'")),
            sa.Column("dim_value", sa.String(1024), nullable=False, server_default=sa.text("''")),
            sa.Column("rewrite_mode", sa.String(16), nullable=False, server_default=sa.text("'expand'")),
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
            sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'manual'")),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'draft'")),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
            sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("remark", sa.String(512), nullable=False, server_default=sa.text("''")),
            sa.Column("created_by", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("updated_by", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "scope", "tenant_id", "user_id", "entry_type", "term", name="uk_slang"
            ),
            mysql_charset="utf8mb4",
        )
    for ix, col in (
        ("ix_slang_entries_scope", "scope"),
        ("ix_slang_entries_tenant_id", "tenant_id"),
        ("ix_slang_entries_user_id", "user_id"),
        ("ix_slang_entries_term", "term"),
        ("ix_slang_entries_entry_type", "entry_type"),
        ("ix_slang_entries_source", "source"),
        ("ix_slang_entries_status", "status"),
    ):
        if not _index_exists("slang_entries", ix):
            op.create_index(ix, "slang_entries", [col])

    if not _table_exists("slang_entry_versions"):
        op.create_table(
            "slang_entry_versions",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("entry_id", sa.BigInteger(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("change_type", sa.String(16), nullable=False, server_default=sa.text("'update'")),
            sa.Column("before_json", sa.JSON(), nullable=True),
            sa.Column("after_json", sa.JSON(), nullable=True),
            sa.Column("operator", sa.String(64), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            mysql_charset="utf8mb4",
        )
    if not _index_exists("slang_entry_versions", "ix_slang_entry_versions_entry_id"):
        op.create_index("ix_slang_entry_versions_entry_id", "slang_entry_versions", ["entry_id"])

    # ---- 数据搬迁：旧 cant_entries -> 新 slang_entries（幂等） ----
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
                    '由 cant_entries 迁移（0010）',
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


def downgrade() -> None:
    if _table_exists("slang_entry_versions"):
        op.drop_table("slang_entry_versions")
    if _table_exists("slang_entries"):
        op.drop_table("slang_entries")
