"""记忆分层 L1 迁 Redis + L3 领域知识表

Revision ID: 0006_memory_l1_l3
Revises: 0005_few_shot_bad_case
Create Date: 2026-09-13

对齐 models.py 的两处改动：

1. memory_summaries（L1 会话滚动摘要）增加两个热层字段：
   - expires_at        DATETIME NULL  —— Redis 热层 TTL 到期时刻（遗忘与命中续期用）
   - last_accessed_at  DATETIME NULL  —— 最近命中时间（时效衰减用）

2. 新建 domain_memories（L3 领域知识记忆，团队 / 域级，长期沉淀，审核制）：
   - 点赞样本库 few_shot_examples 视为本表的「已验证样本」视图（type='few_shot' &
     status='approved'）；二者在意图识别阶段合并做 BM25 召回重排。
   - 还承载更泛化的规则 / 术语 / 纠错样本（type=rule|glossary|correction），
     由管理员审核后生效（status=pending|approved|rejected）。

MySQL 注意：
- TEXT / BLOB / JSON 列**不允许字面量 server_default**（错误 1101）。本迁移中 question /
  answer / tags 为 TEXT 列，因此不在 DDL 里给默认值；默认值由 ORM 的 Python 侧
  default="" 提供（见 models.DomainMemoryModel）。VARCHAR 列（domain/type/status/
  created_by）与数值列（weight）可安全使用字面量 server_default。
- 本迁移写成幂等：列 / 表已存在则跳过（CREATE / ADD COLUMN 不重复执行），避免迁移
  中途失败后重跑报 "Duplicate column"；downgrade 也按存在性删除，避免表未建时
  drop_table 报错。

执行：在 backend/ 目录下 `alembic upgrade head`
（本地开发 auto_create_tables=True 已由 ORM metadata.create_all 自动建表，无需手动迁移）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_memory_l1_l3"
down_revision: Union[str, None] = "0005_few_shot_bad_case"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    """返回 inspector；离线渲染模式（无真实连接）探测失败返回 None。"""
    try:
        return sa.inspect(op.get_bind())
    except Exception:  # noqa: BLE001 - 离线/无连接时不阻塞，交由调用方按"不存在"处理
        return None


def _table_exists(name: str) -> bool:
    insp = _inspector()
    if insp is None:
        return False
    return name in insp.get_table_names()


def _column_exists(table: str, name: str) -> bool:
    insp = _inspector()
    if insp is None:
        return False
    return any(c["name"] == name for c in insp.get_columns(table))


def upgrade() -> None:
    # 1) L1 会话摘要热层字段（新增列，可空，不影响既有数据；已存在则跳过，支持重跑）
    if not _column_exists("memory_summaries", "expires_at"):
        op.add_column(
            "memory_summaries",
            sa.Column("expires_at", sa.DateTime(), nullable=True),
        )
    if not _column_exists("memory_summaries", "last_accessed_at"):
        op.add_column(
            "memory_summaries",
            sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        )

    # 2) L3 领域知识记忆表
    #    注意：question / answer / tags 为 TEXT 列，MySQL 不允许字面量默认值，故不带
    #    server_default（默认值由 ORM Python 侧 default="" 提供）。
    if not _table_exists("domain_memories"):
        op.create_table(
            "domain_memories",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("domain", sa.String(32), nullable=False, server_default=sa.text("'metric_query'")),
            # few_shot | rule | glossary | correction
            sa.Column("type", sa.String(32), nullable=False, server_default=sa.text("'few_shot'")),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("answer", sa.Text(), nullable=False),
            sa.Column("intent", sa.String(32), nullable=True),
            sa.Column("tags", sa.Text(), nullable=False),
            # 频次 / 审核加权（召回重排用）
            sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
            # pending | approved | rejected（管理员审核；点赞沉淀默认 approved）
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'approved'")),
            sa.Column("created_by", sa.String(64), nullable=False, server_default=sa.text("'system'")),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.Index("ix_domain_memories_domain", "domain"),
            sa.Index("ix_domain_memories_type", "type"),
            sa.Index("ix_domain_memories_intent", "intent"),
            sa.Index("ix_domain_memories_status", "status"),
            sa.Index("ix_domain_memories_created_at", "created_at"),
        )


def downgrade() -> None:
    # 表可能未创建（迁移中途失败），存在才删
    if _table_exists("domain_memories"):
        op.drop_table("domain_memories")
    if _column_exists("memory_summaries", "last_accessed_at"):
        op.drop_column("memory_summaries", "last_accessed_at")
    if _column_exists("memory_summaries", "expires_at"):
        op.drop_column("memory_summaries", "expires_at")
