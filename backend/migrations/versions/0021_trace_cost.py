"""FR11 成本可观测：trace_records 增加归属与成本列。

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-18

支撑「按模型 / 用户 / 租户 / 意图」的成本归集（`GET /obs/cost`）：
- user_id / tenant_id：成本按人 / 按租户归集（历史行回填空串）
- model：本轮使用的 LLM 模型名，成本按模型归集看板的核心分组键
- cost：本轮估算成本（币种 DMF_COST-CURRENCY，默认 CNY；未启用计价为 0）

全部为可空 NOT NULL + server_default 加列，历史行自动回填，无破坏性变更。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("user_id", sa.String(length=64), ""),
    ("tenant_id", sa.String(length=64), ""),
    ("model", sa.String(length=64), ""),
    ("cost", sa.Float(), "0"),
)


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001
        return None


def _column_exists(table: str, column: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(
            c.get("name") == column for c in insp.get_columns(table)
        )
    except Exception:  # noqa: BLE001
        return False


def _index_exists(table: str, index: str) -> bool:
    insp = _inspector()
    try:
        return bool(insp) and any(
            i.get("name") == index for i in insp.get_indexes(table)
        )
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        if _column_exists("trace_records", name):
            continue
        op.add_column(
            "trace_records",
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    # 索引只建在分组键上（cost 不做索引，避免写放大）
    for name in ("user_id", "tenant_id", "model"):
        idx = f"ix_trace_records_{name}"
        if not _index_exists("trace_records", idx):
            op.create_index(idx, "trace_records", [name])


def downgrade() -> None:
    for name in ("user_id", "tenant_id", "model"):
        idx = f"ix_trace_records_{name}"
        if _index_exists("trace_records", idx):
            op.drop_index(idx, table_name="trace_records")
    for name, _, _ in reversed(_COLUMNS):
        if _column_exists("trace_records", name):
            op.drop_column("trace_records", name)
