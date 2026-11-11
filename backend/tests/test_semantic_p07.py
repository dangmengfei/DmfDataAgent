"""P0-7（FR1 收尾）测试：时间序列「组内排名」+ 同环比「同比·环比并存」。

覆盖三层：
1. 要素层——`detect_rank_scope` 判定与 `_infer_pop_types` 多类型收集（含最长匹配防误判）；
2. 计划层——`build_dsl` 透传 + `MetricQuery.from_dsl` 归一（去重/规范顺序/算子映射）；
3. 编译层——组内排名 `PARTITION BY <时间列>`、组内 TopN 走名次列过滤、多对比期窗口。
"""

from dmf_agent.agents.metric_agent.nodes.analyze_data import _generate_advanced_rules
from dmf_agent.agents.metric_agent.nodes.build_dsl import build_build_dsl_node
from dmf_agent.agents.metric_agent.nodes.extract_elements import (
    _infer_pop_type,
    _infer_pop_types,
    _rule_based_extract,
)
from dmf_agent.agents.metric_agent.sorting import detect_rank_scope, parse_sort
from dmf_agent.config.settings import Settings
from dmf_agent.semantic import MetricFlowSqlCompiler, SemanticLayer
from dmf_agent.semantic.query import MetricQuery

from fakes import make_deps

layer = SemanticLayer()
compiler = MetricFlowSqlCompiler(layer, Settings().starrocks)

# 月粒度窗口：对比期可确定性断言（同比 -12 月 / 环比 -1 月）
_AUG = {"start": "2026-08-01", "end": "2026-08-31", "granularity": "month"}
# 半年窗口（组内排名按月的多期场景）
_HALF_YEAR = {"start": "2026-03-01", "end": "2026-08-31", "granularity": "month"}


def _compile(dsl):
    return compiler.compile(MetricQuery.from_dsl(dsl, layer))


# ---- 1. 排名范围判定 ----


def test_detect_rank_scope_group_variants():
    assert detect_rank_scope("各分行每月的存款余额排名") == "group"
    assert detect_rank_scope("按周排名各渠道销量") == "group"
    assert detect_rank_scope("各月排名前3的网点") == "group"
    assert detect_rank_scope("取每月前3的网点") == "group"
    assert detect_rank_scope("每月最高的网点") == "group"
    assert detect_rank_scope("每个月的组内排名") == "group"


def test_detect_rank_scope_global_and_no_rank():
    # 全局排名
    assert detect_rank_scope("GMV最高的前10个门店") == "global"
    assert detect_rank_scope("各分行存款余额排名") == "global"
    # 纯时间序列（无名次诉求）不得误判为组内排名
    assert detect_rank_scope("近半年贷款余额月度趋势与环比") == "global"
    assert detect_rank_scope("各月GMV走势") == "global"
    assert detect_rank_scope("") == "global"


def test_parse_sort_carries_rank_scope():
    spec = parse_sort("各分行每月的存款余额排名", ["零售存款余额"], ["统计日期"])
    assert spec is not None
    assert spec.rank_scope == "group"
    assert spec.order_by == "零售存款余额 desc"
    plain = parse_sort("各月GMV走势", ["GMV"])
    assert plain is None or plain.rank_scope == "global"


# ---- 2. 同环比多类型收集 ----


def test_infer_pop_types_single_and_dual():
    assert _infer_pop_types("GMV较去年同期") == ["yoy"]
    assert _infer_pop_types("GMV环比") == ["mom"]
    # 同比 + 环比并存 -> 规范顺序 yoy 在前
    assert _infer_pop_types("本月GMV和上月比、也和去年同期比") == ["yoy", "mom"]
    assert _infer_pop_types("GMV同比环比都看") == ["yoy", "mom"]
    assert _infer_pop_types("近7天GMV按渠道") == []


def test_infer_pop_types_longest_match_avoids_double_count():
    """"周同比"整体算 wow，内部"同比"不再重复记为 yoy。"""
    assert _infer_pop_types("本周销量周同比") == ["wow"]
    assert _infer_pop_types("GMV季同比") == ["qoq"]
    assert _infer_pop_type("GMV季同比") == "qoq"


# ---- 3. 编译层：组内排名 ----


def test_compile_group_rank_partitions_by_time():
    compiled = _compile(
        {
            "metrics": [{"name": "GMV"}],
            "dimensions": ["统计日期", "门店"],
            "time_range": dict(_HALF_YEAR),
            "order_by": "GMV desc",
            "limit": 3,
            "rank_scope": "group",
        }
    )
    sql = compiled.sql
    # 每期内部各自排名
    assert "ROW_NUMBER() OVER (PARTITION BY `date` ORDER BY `GMV` DESC)" in sql
    # 组内 TopN 按名次列过滤，不落全局 LIMIT
    assert "`GMV__排名` <= 3" in sql
    assert "LIMIT 3" not in sql
    # 时间轴在前、名次在后，便于逐期阅读
    assert "ORDER BY `date` DESC, `GMV__排名` ASC" in sql
    assert "GMV__排名" in compiled.columns


def test_compile_global_rank_keeps_global_limit():
    compiled = _compile(
        {
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "time_range": dict(_HALF_YEAR),
            "order_by": "GMV desc",
            "limit": 3,
            "rank_scope": "global",
        }
    )
    sql = compiled.sql
    assert "PARTITION BY" not in sql
    assert "GMV__排名" not in compiled.columns
    assert sql.rstrip().endswith("LIMIT 3")


def test_compile_group_rank_via_advanced_ranking_method():
    """LLM/老通道的 advanced.ranking_method=group 同样生效（不再被静默丢弃）。"""
    dsl = {
        "metrics": [{"name": "GMV"}],
        "dimensions": ["统计日期", "门店"],
        "time_range": dict(_HALF_YEAR),
        "advanced": {
            "metric_result": [{"metric_name": "GMV", "orderby": "desc"}],
            "ranking_method": "group",
        },
    }
    query = MetricQuery.from_dsl(dsl, layer)
    assert query.rank_scope == "group"
    assert query.metric_ops["GMV"].rank_group is True
    assert "PARTITION BY `date`" in compiler.compile(query).sql


def test_compile_group_rank_without_time_dim_falls_back_to_global():
    """组内排名但分组里没有时间维度时降级为全局名次（不报错、不缺列）。"""
    compiled = _compile(
        {
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "time_range": dict(_HALF_YEAR),
            "rank_scope": "group",
        }
    )
    sql = compiled.sql
    assert "PARTITION BY" not in sql
    assert "`GMV__排名` ASC" in sql


# ---- 4. 编译层：同比 + 环比并存 ----


def test_compile_multi_period_comparison_windows_and_aliases():
    compiled = _compile(
        {
            "metrics": [{"name": "零售存款余额"}],
            "dimensions": ["统计日期"],
            "time_range": dict(_AUG),
            "period_comparison": True,
            "period_comparison_types": ["yoy", "mom"],
        }
    )
    sql = compiled.sql
    # 两个对比期各自平移时间窗：同比 -> 去年 8 月，环比 -> 今年 7 月
    assert "'2025-08-01'" in sql and "'2025-08-31'" in sql
    assert "'2026-07-01'" in sql and "'2026-07-31'" in sql
    # 当期分别 join 两个对比期（join 方式随 settings.join_type/方言：starrocks 默认 FULL OUTER）
    assert " p_joined AS p ON" in sql
    assert " p2_joined AS p2 ON" in sql
    cols = set(compiled.columns)
    for suffix in ("yoy", "mom"):
        for kind in ("前期", "增长值", "增长率"):
            assert f"零售存款余额__{kind}_{suffix}" in cols


def test_compile_single_period_comparison_keeps_legacy_alias():
    """单类型必须保持历史列名（后端/前端既有消费方零改动）。"""
    compiled = _compile(
        {
            "metrics": [{"name": "零售存款余额"}],
            "dimensions": ["统计日期"],
            "time_range": dict(_AUG),
            "period_comparison": True,
            "period_comparison_types": ["yoy"],
        }
    )
    cols = set(compiled.columns)
    assert {"零售存款余额__前期", "零售存款余额__增长值", "零售存款余额__增长率"} <= cols
    assert "零售存款余额__增长率_yoy" not in cols
    assert "p2_" not in compiled.sql


def test_metric_query_from_dsl_normalizes_pop_types():
    query = MetricQuery.from_dsl(
        {
            "metrics": [{"name": "GMV"}],
            "group_by": ["统计日期"],
            "period_comparison": True,
            "period_comparison_types": ["mom", "yoy", "mom"],
        },
        layer,
    )
    assert query.period_comparison is True
    assert query.period_comparison_types == ["yoy", "mom"]  # 去重 + 规范顺序
    assert query.period_comparison_type == "yoy"


def test_from_dsl_pop_type_singular_fallback():
    """只给旧字段 period_comparison_type 时仍可用（向后兼容）。"""
    query = MetricQuery.from_dsl(
        {
            "metrics": [{"name": "GMV"}],
            "group_by": ["统计日期"],
            "period_comparison": True,
            "period_comparison_type": "mom",
        },
        layer,
    )
    assert query.period_comparison_types == ["mom"]


# ---- 5. 语义层：多类型白名单校验 ----


def test_validate_elements_multi_pop_reports_unsupported_type():
    problems = layer.validate_elements(
        {
            "metrics": ["日活跃用户数"],
            "dimensions": [],
            "time_range": {"granularity": "month"},
            "period_comparison": True,
            "period_comparison_types": ["yoy", "qoq"],
        }
    )
    assert len(problems) == 1 and "qoq" in problems[0]
    ok = layer.validate_elements(
        {
            "metrics": ["日活跃用户数"],
            "dimensions": [],
            "time_range": {"granularity": "month"},
            "period_comparison": True,
            "period_comparison_types": ["yoy", "mom"],
        }
    )
    assert ok == []


# ---- 6. 规则抽取 + 计划透传 ----


async def test_rule_extract_group_rank_and_dual_pop():
    deps = make_deps()
    ranked = await _rule_based_extract("各分行每月的存款余额排名", deps)
    assert "零售存款余额" in ranked["metrics"]
    assert ranked["rank_scope"] == "group"
    assert ranked["order_by"] == "零售存款余额 desc"

    dual = await _rule_based_extract("本月零售存款余额和上月比、也和去年同期比", deps)
    assert dual["period_comparison"] is True
    assert dual["period_comparison_types"] == ["yoy", "mom"]
    assert dual["period_comparison_type"] == "yoy"

    single = await _rule_based_extract("GMV较去年同期", deps)
    assert single["period_comparison_types"] == ["yoy"]

    plain = await _rule_based_extract("近7天GMV按渠道", deps)
    assert plain["period_comparison_types"] == []
    assert plain["rank_scope"] == "global"


async def test_build_dsl_passes_rank_scope_and_pop_types():
    deps = make_deps()
    elements = await _rule_based_extract("各分行每月存款余额排名，和上月比", deps)
    dsl = (await build_build_dsl_node(deps)({"query_elements": elements}))["dsl"]
    assert dsl["rank_scope"] == "group"
    assert dsl["period_comparison_types"] == ["mom"]


def test_advanced_rules_detect_group_ranking():
    group = _generate_advanced_rules(
        "各分行每月存款余额排名",
        {"metrics": ["零售存款余额"], "dimensions": ["统计日期", "机构"]},
    )
    assert group["ranking_method"] == "group"
    global_rank = _generate_advanced_rules(
        "GMV排名前10的门店", {"metrics": ["GMV"], "dimensions": ["门店"]}
    )
    assert global_rank["ranking_method"] == "global"
    none = _generate_advanced_rules(
        "近7天各渠道GMV", {"metrics": ["GMV"], "dimensions": ["渠道"]}
    )
    assert none["ranking_method"] == ""
