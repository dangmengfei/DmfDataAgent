"""语义注册中心（P1 骨架）：模型 / 维度 / 度量 / 指标注册表 + 指标版本表

Revision ID: 0013_semantic_registry
Revises: 0012_slang_rewrite_events
Create Date: 2026-09-14

对应 doc/语义层注册中心与指标权限治理方案.md §3.1：
- `semantic_models` / `semantic_dimensions` / `semantic_measures` / `semantic_metrics`：
  manifest.yml 的等价镜像（P1 只读 + 启动幂等导入种子；P3 切换为 DB 真源 + 热更新）；
- `semantic_metric_versions`：指标版本审计（P2 状态机 / 回滚用，P1 随导入写首版快照）；
- `*_key` 为不可变稳定身份（权限 / 血缘 / 审计锚点），`name` / `aliases` 可改。

注意：本方案原稿预留的迁移编号 0011 已被 `0011_drop_cant_entries`（cant_entries 清理）
占用，0012 为改写埋点表；故本迁移顺延为 **0013**。

执行：backend/ 下 `alembic upgrade head`
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_semantic_registry"
down_revision: Union[str, None] = "0012_slang_rewrite_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 表 -> 唯一键（幂等守卫用）
_TABLES = ("semantic_metrics", "semantic_measures", "semantic_dimensions", "semantic_models")


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
    if not _table_exists("semantic_models"):
        op.create_table(
            "semantic_models",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="industry"),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("model_key", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("table", sa.String(length=255), nullable=False),
            sa.Column("agg_time_dimension", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("entities", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="published"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="builtin"),
            sa.Column("owner", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope", "tenant_id", "model_key", name="uk_sem_model"),
        )
        for idx, col in (
            ("ix_semantic_models_scope", "scope"),
            ("ix_semantic_models_tenant_id", "tenant_id"),
            ("ix_semantic_models_model_key", "model_key"),
            ("ix_semantic_models_status", "status"),
            ("ix_semantic_models_source", "source"),
        ):
            if not _index_exists("semantic_models", idx):
                op.create_index(idx, "semantic_models", [col])

    if not _table_exists("semantic_dimensions"):
        op.create_table(
            "semantic_dimensions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("model_id", sa.Integer(), nullable=False),
            sa.Column("dimension_key", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("dim_type", sa.String(length=16), nullable=False, server_default="categorical"),
            sa.Column("expr", sa.String(length=255), nullable=False),
            sa.Column("time_granularity", sa.String(length=16), nullable=False, server_default="day"),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="published"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("model_id", "dimension_key", name="uk_sem_dimension"),
        )
        for idx, col in (
            ("ix_semantic_dimensions_model_id", "model_id"),
            ("ix_semantic_dimensions_name", "name"),
            ("ix_semantic_dimensions_status", "status"),
        ):
            if not _index_exists("semantic_dimensions", idx):
                op.create_index(idx, "semantic_dimensions", [col])

    if not _table_exists("semantic_measures"):
        op.create_table(
            "semantic_measures",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("model_id", sa.Integer(), nullable=False),
            sa.Column("measure_key", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("agg", sa.String(length=32), nullable=False, server_default="sum"),
            sa.Column("expr", sa.String(length=255), nullable=False),
            sa.Column("agg_time_dimension", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="published"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("model_id", "measure_key", name="uk_sem_measure"),
        )
        for idx, col in (
            ("ix_semantic_measures_model_id", "model_id"),
            ("ix_semantic_measures_status", "status"),
        ):
            if not _index_exists("semantic_measures", idx):
                op.create_index(idx, "semantic_measures", [col])

    if not _table_exists("semantic_metrics"):
        op.create_table(
            "semantic_metrics",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="industry"),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("metric_key", sa.String(length=255), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("metric_type", sa.String(length=16), nullable=False, server_default="simple"),
            sa.Column("type_params", sa.JSON(), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("scale", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("round", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("granularities", sa.JSON(), nullable=False),
            sa.Column("pop_types", sa.JSON(), nullable=False),
            sa.Column("owner", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="published"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="builtin"),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope", "tenant_id", "metric_key", name="uk_sem_metric"),
        )
        for idx, col in (
            ("ix_semantic_metrics_scope", "scope"),
            ("ix_semantic_metrics_tenant_id", "tenant_id"),
            ("ix_semantic_metrics_metric_key", "metric_key"),
            ("ix_semantic_metrics_name", "name"),
            ("ix_semantic_metrics_metric_type", "metric_type"),
            ("ix_semantic_metrics_status", "status"),
            ("ix_semantic_metrics_source", "source"),
        ):
            if not _index_exists("semantic_metrics", idx):
                op.create_index(idx, "semantic_metrics", [col])

    if not _table_exists("semantic_metric_versions"):
        op.create_table(
            "semantic_metric_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("metric_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("change_type", sa.String(length=16), nullable=False, server_default="import"),
            sa.Column("before_json", sa.JSON(), nullable=True),
            sa.Column("after_json", sa.JSON(), nullable=True),
            sa.Column("operator", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        if not _index_exists("semantic_metric_versions", "ix_semantic_metric_versions_metric_id"):
            op.create_index(
                "ix_semantic_metric_versions_metric_id",
                "semantic_metric_versions",
                ["metric_id"],
            )


def downgrade() -> None:
    # 维度/度量挂在模型下，先删子表
    op.drop_index(
        "ix_semantic_metric_versions_metric_id",
        table_name="semantic_metric_versions",
    )
    op.drop_table("semantic_metric_versions")
    for idx in (
        "ix_semantic_metrics_source",
        "ix_semantic_metrics_status",
        "ix_semantic_metrics_metric_type",
        "ix_semantic_metrics_name",
        "ix_semantic_metrics_metric_key",
        "ix_semantic_metrics_tenant_id",
        "ix_semantic_metrics_scope",
    ):
        if _index_exists("semantic_metrics", idx):
            op.drop_index(idx, table_name="semantic_metrics")
    op.drop_table("semantic_metrics")
    for idx in (
        "ix_semantic_measures_status",
        "ix_semantic_measures_model_id",
    ):
        if _index_exists("semantic_measures", idx):
            op.drop_index(idx, table_name="semantic_measures")
    op.drop_table("semantic_measures")
    for idx in (
        "ix_semantic_dimensions_status",
        "ix_semantic_dimensions_name",
        "ix_semantic_dimensions_model_id",
    ):
        if _index_exists("semantic_dimensions", idx):
            op.drop_index(idx, table_name="semantic_dimensions")
    op.drop_table("semantic_dimensions")
    for idx in (
        "ix_semantic_models_source",
        "ix_semantic_models_status",
        "ix_semantic_models_model_key",
        "ix_semantic_models_tenant_id",
        "ix_semantic_models_scope",
    ):
        if _index_exists("semantic_models", idx):
            op.drop_index(idx, table_name="semantic_models")
    op.drop_table("semantic_models")
