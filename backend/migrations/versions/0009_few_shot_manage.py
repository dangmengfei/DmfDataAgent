"""few_shot_examples 管理字段（Few-Shot 管理页）

Revision ID: 0009_few_shot_manage
Revises: 0008_preferences_doc_tenant
Create Date: 2026-09-14

对齐 models.py::FewShotExampleModel —— 为「Few-Shot 管理」页补 4 列（纯增量，不改既有列）：

1. `source`  VARCHAR(16) NOT NULL DEFAULT 'like'
   样本来源：like=点赞自动沉淀 / manual=管理面手工录入或批量导入。
   存量行全部回填为 'like'（本列新增前只有点赞一条写入路径）。

2. `status`  VARCHAR(16) NOT NULL DEFAULT 'active'
   active | disabled。停用的样本不参与召回（`MemoryRetriever` 只取 active），
   但保留记录以便审计与恢复 —— 管理页"停用"而非"删除"。

3. `weight`  FLOAT NOT NULL DEFAULT 1.0
   召回重排权重（与 domain_memories.weight 同义），用于人工提升某些示例的优先级。

4. `tags`    TEXT NOT NULL
   逗号分隔标签（管理页筛选与备注）。TEXT 列不能带字面量默认值（MySQL 1101），
   默认值由 ORM 提供，DDL 里给空串默认以便存量行非空。

另补 `source` / `status` 两个索引（管理页按来源与状态过滤）。

幂等：列 / 索引已存在则跳过（中途失败后重跑不报 Duplicate column）。
`session_id` / `message_id` 同时放宽为"可为空串"——手工样本没有原始消息，
该语义由 ORM 的 default="" 承担，DDL 无需变更（原列本就 NOT NULL 且无默认，
这里显式补 server_default '' 以便手工 INSERT 不写这两列时不报错）。

执行：在 backend/ 目录下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_few_shot_manage"
down_revision: Union[str, None] = "0008_preferences_doc_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001 - 离线渲染无连接，按"不存在"处理照常 emit DDL
        return None


def _column_exists(table: str, name: str) -> bool:
    insp = _inspector()
    return bool(insp) and any(c["name"] == name for c in insp.get_columns(table))


def _index_exists(table: str, name: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        return any(ix["name"] == name for ix in insp.get_indexes(table))
    except Exception:  # noqa: BLE001
        return False


def _column_has_default(table: str, name: str) -> bool:
    insp = _inspector()
    if not insp:
        return False
    try:
        for c in insp.get_columns(table):
            if c["name"] == name:
                return c.get("default") is not None
    except Exception:  # noqa: BLE001
        return False
    return False


def upgrade() -> None:
    if not _column_exists("few_shot_examples", "source"):
        op.add_column(
            "few_shot_examples",
            sa.Column(
                "source", sa.String(16), nullable=False, server_default=sa.text("'like'")
            ),
        )
    if not _index_exists("few_shot_examples", "ix_few_shot_examples_source"):
        op.create_index("ix_few_shot_examples_source", "few_shot_examples", ["source"])

    if not _column_exists("few_shot_examples", "status"):
        op.add_column(
            "few_shot_examples",
            sa.Column(
                "status", sa.String(16), nullable=False, server_default=sa.text("'active'")
            ),
        )
    if not _index_exists("few_shot_examples", "ix_few_shot_examples_status"):
        op.create_index("ix_few_shot_examples_status", "few_shot_examples", ["status"])

    if not _column_exists("few_shot_examples", "weight"):
        op.add_column(
            "few_shot_examples",
            sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        )

    if not _column_exists("few_shot_examples", "tags"):
        # TEXT 不能带字面量默认值（MySQL 1101），默认值由 ORM 提供；
        # ADD COLUMN NOT NULL 无默认值时 MySQL 用隐式默认（TEXT 为 ''）回填存量行。
        op.add_column(
            "few_shot_examples",
            sa.Column("tags", sa.Text(), nullable=False),
        )

    # 手工样本没有原始消息：补 server_default '' 让 INSERT 省略这两列也不报错
    for col in ("session_id", "message_id"):
        if not _column_has_default("few_shot_examples", col):
            try:
                op.alter_column(
                    "few_shot_examples", col,
                    existing_type=sa.String(64), nullable=False,
                    server_default=sa.text("''"),
                )
            except Exception:  # noqa: BLE001 - 方言差异下不阻断迁移
                pass


def downgrade() -> None:
    for ix in ("ix_few_shot_examples_status", "ix_few_shot_examples_source"):
        if _index_exists("few_shot_examples", ix):
            try:
                op.drop_index(ix, table_name="few_shot_examples")
            except Exception:  # noqa: BLE001
                pass
    for col in ("tags", "weight", "status", "source"):
        if _column_exists("few_shot_examples", col):
            op.drop_column("few_shot_examples", col)
