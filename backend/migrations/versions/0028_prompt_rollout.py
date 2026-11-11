"""提示词 FR12：5 层标注 + 灰度 + 评测门禁。

prompt_versions 增加 4 列：
- layer           VARCHAR(16)  5 层之一：system/task/tool/postprocess/security（默认 ''）
- rollout_status  VARCHAR(16)  灰度状态：full/canary/disabled（默认 full）
- gray_ratio      FLOAT        金丝雀命中比例 0~1（默认 1.0）
- eval_passed     BOOLEAN      评测门禁是否通过（默认 false）

幂等守卫：列已存在则跳过，避免重复迁移报错。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def upgrade() -> None:
    if not _column_exists("prompt_versions", "layer"):
        op.add_column(
            "prompt_versions",
            sa.Column("layer", sa.String(16), nullable=False, server_default=""),
        )
    if not _column_exists("prompt_versions", "rollout_status"):
        op.add_column(
            "prompt_versions",
            sa.Column("rollout_status", sa.String(16), nullable=False, server_default="full"),
        )
    if not _column_exists("prompt_versions", "gray_ratio"):
        op.add_column(
            "prompt_versions",
            sa.Column("gray_ratio", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        )
    if not _column_exists("prompt_versions", "eval_passed"):
        op.add_column(
            "prompt_versions",
            sa.Column("eval_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    for col in ("eval_passed", "gray_ratio", "rollout_status", "layer"):
        if _column_exists("prompt_versions", col):
            op.drop_column("prompt_versions", col)
