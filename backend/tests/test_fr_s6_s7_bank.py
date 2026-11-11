"""S6 / S7：银行域两阶段聚合（月日均）、月末时点值（last）的指标落库与 SQL 编译验证。

验证目标：
- S6：月日均存款余额 = 先按天 SUM 各维度余额、再按月 AVG 每日余额（非底表行均值）。
- S7：期末存款余额 = 先按天 SUM、再取目标粒度内最后一天的值（两阶段 sum→last，月末即月末快照）。
- 同一 measure 上的两个 two_stage 指标（avg / last）必须生成独立列，不互相覆盖。
"""

from dmf_agent.config.settings import Settings
from dmf_agent.semantic import MetricFlowSqlCompiler, SemanticLayer
from dmf_agent.semantic.manifest import SemanticManifest
from dmf_agent.semantic.query import MetricQuery

layer = SemanticLayer()
compiler = MetricFlowSqlCompiler(layer, Settings().starrocks)


def _compile(metric_names, dims, start, end, granularity):
    return compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": n} for n in metric_names],
                "dimensions": dims,
                "time_range": {"start": start, "end": end, "granularity": granularity},
            },
            layer,
        )
    ).sql


def test_bank_manifest_loads_two_stage_metrics():
    """真实 manifest.d 能加载并解析三个两阶段银行指标。"""
    for name in ("月日均存款余额", "期末存款余额", "月日均存款客户数"):
        assert layer.resolve_metric(name) is not None, f"未收录两阶段指标：{name}"


def test_two_stage_avg_compiles_on_real_bank_metric():
    """S6：月日均存款余额编译为「按天 SUM -> 按月 AVG」。"""
    sql = _compile(["月日均存款余额"], ["统计日期"], "2026-09-01", "2026-09-30", "month")
    # 日内子查询：按天 SUM 余额（两阶段列用 __ts 后缀区分）
    assert "SUM(t.deposit_balance) AS `m_retail_deposit_balance_avg_sum__ts__day`" in sql
    # 外层：对每日余额再求均值（目标粒度）
    assert (
        "AVG(sub.`m_retail_deposit_balance_avg_sum__ts__day`)"
        " AS `m_retail_deposit_balance_avg_sum__ts`"
        in sql
    )


def test_two_stage_last_compiles_on_real_bank_metric():
    """S7：期末存款余额编译为「按天 SUM -> 取最后一天值 ARG_MAX(每日SUM, __day)」。"""
    sql = _compile(["期末存款余额"], ["统计日期"], "2026-09-01", "2026-09-30", "month")
    # 日内子查询：按天 SUM 余额（last 外层用 __ts_last 后缀避免与 avg 列碰撞）
    assert (
        "SUM(t.deposit_balance) AS `m_retail_deposit_balance_eom_sum__ts_last__day`" in sql
    )
    # 外层 last -> ARG_MAX(每日SUM, __day) 取月末快照值
    assert (
        "ARG_MAX(sub.`m_retail_deposit_balance_eom_sum__ts_last__day`, sub.`__day`)"
        " AS `m_retail_deposit_balance_eom_sum__ts_last`"
        in sql
    )


def test_two_stage_avg_and_last_same_measure_distinct_columns():
    """两个不同 measure 的两阶段指标（avg / last）必须生成独立列（不碰撞）。"""
    sql = _compile(
        ["月日均存款余额", "期末存款余额"], ["统计日期"], "2026-09-01", "2026-09-30", "month"
    )
    assert (
        "AVG(sub.`m_retail_deposit_balance_avg_sum__ts__day`)"
        " AS `m_retail_deposit_balance_avg_sum__ts`"
        in sql
    )
    assert (
        "ARG_MAX(sub.`m_retail_deposit_balance_eom_sum__ts_last__day`, sub.`__day`)"
        " AS `m_retail_deposit_balance_eom_sum__ts_last`"
        in sql
    )


def test_two_stage_last_validation_accepted():
    """清单校验器应接受 two_stage.outer = last（不再报"聚合方式不支持"）。"""
    raw = layer.manifest.to_dict()
    # 字典往返后新指标的 two_stage 仍保留，校验应通过
    assert SemanticManifest.from_dict(raw).validate() == []
    assert SemanticManifest.from_dict(raw).reference_problems() == []
