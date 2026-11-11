"""语义层测试：语义清单校验 + 指标/维度解析 + MetricFlow SQL 编译 + 只读守卫 + API。"""

import pytest

from dmf_agent.config.settings import Settings
from dmf_agent.semantic import (
    CompileError,
    MetricFlowEngine,
    MetricFlowSqlCompiler,
    SemanticLayer,
    StarRocksClient,
    assert_read_only,
)
from dmf_agent.semantic.compiler import lag_delta
from dmf_agent.semantic.manifest import Metric
from dmf_agent.semantic.query import MetricQuery
from dmf_agent.semantic.starrocks import StarRocksError

layer = SemanticLayer()
compiler = MetricFlowSqlCompiler(layer, Settings().starrocks)


# ---- 语义清单 ----


def test_manifest_valid():
    assert layer.manifest.validate() == []
    assert layer.manifest.reference_problems() == []


def test_two_semantic_models_with_tables():
    models = {m.name: m.table for m in layer.manifest.models}
    assert models["trade_day"] == "dws_trade_day"
    assert models["user_day"] == "dws_user_day"


# ---- 指标解析 ----


def test_list_metrics():
    names = layer.metric_names()
    assert "GMV" in names and "客单价" in names and "付费转化率" in names


def test_resolve_metric_by_name_and_alias():
    assert layer.resolve_metric("GMV").name == "GMV"
    # 别名命中（业务俗称 -> 标准指标）
    assert layer.resolve_metric("成交额").name == "GMV"
    assert layer.resolve_metric("日活").name == "日活跃用户数"
    assert layer.resolve_metric("客单值").name == "客单价"


def test_resolve_metric_unknown_none():
    assert layer.resolve_metric("不存在的指标") is None
    assert layer.resolve_metric("") is None


def test_resolve_dimension():
    dim = layer.resolve_dimension("渠道")
    assert dim is not None and dim.expr == "channel_name"
    assert layer.resolve_dimension("不存在") is None


def test_supported_dimensions_follow_semantic_model():
    # 付费转化率跨 trade_day + user_day，只共有时段/渠道/平台
    assert layer.supported_dimensions(layer.resolve_metric("付费转化率")) == [
        "统计日期",
        "渠道",
        "平台",
    ]
    # 交易域指标支持地区
    assert "地区" in layer.supported_dimensions(layer.resolve_metric("GMV"))


def test_base_measures_expansion():
    assert layer.base_measures("GMV") == ["gmv_amount"]
    assert layer.base_measures("客单价") == ["gmv_amount", "order_count"]
    assert layer.base_measures("付费转化率") == ["paid_user_count", "active_user_count"]


# ---- 白名单校验 ----


def test_validate_metric_support_ok():
    metric = layer.resolve_metric("GMV")
    assert layer.validate_metric_support(metric, granularity="month", pop_type="yoy") == []


def test_validate_metric_support_unsupported_granularity():
    metric = layer.resolve_metric("日活跃用户数")  # 仅 day/week/month
    problems = layer.validate_metric_support(metric, granularity="year")
    assert any("不支持时间粒度 year" in p for p in problems)


def test_validate_metric_support_unsupported_pop():
    metric = layer.resolve_metric("日活跃用户数")  # 支持 mom/yoy/wow/pop，不含 qoq
    problems = layer.validate_metric_support(metric, granularity="day", pop_type="qoq")
    assert any("不支持同环比类型 qoq" in p for p in problems)


def test_validate_elements_unknown_metric_and_dimension():
    problems = layer.validate_elements({"metrics": ["未知指标"]})
    assert any("未收录" in p for p in problems)

    problems = layer.validate_elements(
        {"metrics": ["日活跃用户数"], "dimensions": ["地区"], "time_range": {"granularity": "day"}}
    )
    assert any("所在数据域支持" in p for p in problems)

    assert layer.validate_elements(
        {"metrics": ["GMV"], "dimensions": ["渠道"], "time_range": {"granularity": "day"}}
    ) == []


# ---- SQL 编译 ----


def test_compile_single_model_sql():
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "GMV"}, {"name": "订单数"}],
                "dimensions": ["统计日期", "渠道"],
                "filters": [{"field": "渠道", "operator": "in", "values": ["APP"]}],
                "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
            },
            layer,
        )
    )
    sql = compiled.sql
    assert "FROM `dws_trade_day` AS t" in sql
    assert "SUM(t.gmv) AS `m_gmv_amount_sum`" in sql
    assert "date_trunc('day', t.stat_date) AS `date`" in sql
    assert "t.channel_name IN ('APP')" in sql
    assert "ROUND(c.`m_gmv_amount_sum`, 2) AS `GMV`" in sql
    assert compiled.models == ["trade_day"]
    assert compiled.columns == ["date", "渠道", "GMV", "订单数"]


def test_compile_cross_model_uses_full_outer_join():
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "GMV"}, {"name": "日活跃用户数"}, {"name": "付费转化率"}],
                "dimensions": ["统计日期", "渠道"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
            },
            layer,
        )
    )
    assert "FULL OUTER JOIN c_user_day AS b ON" in compiled.sql
    # ratio 指标在 JOIN 之后计算，并做 *100 百分比换算
    assert "/ NULLIF((c.`m_active_user_count_sum`), 0)) * 100" in compiled.sql
    assert compiled.models == ["trade_day", "user_day"]


def test_compile_derived_metric_expression():
    compiled = compiler.compile(
        MetricQuery.from_dsl({"metrics": [{"name": "客单价"}]}, layer)
    )
    # 客单价 = GMV / 订单数，展开为 measure 层表达式（不嵌套取整）
    assert "/ (c.`m_order_count_sum`)" in compiled.sql
    assert "`m_gmv_amount_sum`" in compiled.sql and "`m_order_count_sum`" in compiled.sql


def test_compile_period_comparison_shifts_window_and_keys():
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "GMV"}],
                "dimensions": ["统计日期"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
                "period_comparison": True,
                "period_comparison_type": "yoy",
            },
            layer,
        )
    )
    sql = compiled.sql
    assert "add_months(t.stat_date, 12)" in sql  # 对比期时间键对齐
    assert "t.stat_date >= '2025-09-01' AND t.stat_date <= '2025-09-07'" in sql
    assert set(["GMV", "GMV__前期", "GMV__增长值", "GMV__增长率"]).issubset(set(compiled.columns))


def test_compile_proportion_and_rank_in_calc_layer():
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "GMV"}],
                "dimensions": ["渠道"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
                "advanced": {
                    "metric_result": [
                        {
                            "metric_name": "GMV",
                            "orderby": "desc",
                            "limit": "3",
                            "proportion": True,
                        }
                    ]
                },
            },
            layer,
        )
    )
    assert "ROUND(`GMV` / NULLIF(SUM(`GMV`) OVER (), 0) * 100, 2) AS `GMV__占比`" in compiled.sql
    assert "ROW_NUMBER() OVER (ORDER BY `GMV` DESC) AS `GMV__排名`" in compiled.sql
    assert compiled.sql.rstrip().endswith("LIMIT 3")


def test_compile_unknown_metric_raises():
    with pytest.raises(CompileError):
        compiler.compile(MetricQuery(metrics=["不存在"], time_dimension="统计日期"))


def test_compile_explicit_order_by_and_limit_from_elements():
    """FR1 排序/TopN：六要素的 order_by + 显式 limit 直接编译为 ORDER BY + LIMIT。"""
    query = MetricQuery.from_dsl(
        {
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
            "order_by": "GMV desc",
            "limit": 10,
        },
        layer,
    )
    assert query.order_by == "GMV desc"
    assert query.limit == 10
    sql = compiler.compile(query).sql
    assert "ORDER BY `GMV` DESC" in sql
    assert sql.rstrip().endswith("LIMIT 10")


def test_from_dsl_limit_coercion_and_suffix_compat():
    # 数字字符串也接受；旧路径 order_by 后缀 "limit N" 仍兼容
    q1 = MetricQuery.from_dsl({"metrics": [{"name": "GMV"}], "limit": "5"}, layer)
    assert q1.limit == 5
    q2 = MetricQuery.from_dsl(
        {"metrics": [{"name": "GMV"}], "order_by": "GMV desc limit 3"}, layer
    )
    assert q2.limit == 3
    q3 = MetricQuery.from_dsl({"metrics": [{"name": "GMV"}], "limit": 0}, layer)
    assert q3.limit is None


def test_compile_dimension_not_in_model_raises():
    with pytest.raises(CompileError):
        compiler.compile(
            MetricQuery(metrics=["日活跃用户数"], group_by=["地区"], time_dimension="统计日期")
        )


def test_compile_period_comparison_without_time_range_raises():
    with pytest.raises(CompileError):
        compiler.compile(
            MetricQuery(
                metrics=["GMV"],
                group_by=["统计日期"],
                period_comparison=True,
                time_dimension="统计日期",
            )
        )


def test_lag_delta_semantics():
    assert lag_delta("yoy", "day") == {"months": 12}
    assert lag_delta("mom", "day") == {"months": 1}
    assert lag_delta("wow", "day") == {"days": 7}
    assert lag_delta("pop", "week") == {"days": 7}
    assert lag_delta("pop", "day") == {"days": 1}


def test_derived_expression_rejects_illegal_chars():
    metric = Metric(
        name="注入指标",
        type="derived",
        type_params={"expr": "GMV; DROP TABLE x", "metrics": ["GMV"]},
    )
    with pytest.raises(CompileError):
        compiler.compile(MetricQuery(metrics=[metric.name]))


def test_compile_dimension_values_sql():
    sql = compiler.compile_dimension_values("渠道", limit=5)
    assert "SELECT DISTINCT channel_name" in sql
    assert "FROM `dws_trade_day`" in sql
    assert sql.rstrip().endswith("LIMIT 5")


# ---- StarRocks 只读守卫 ----


def test_assert_read_only_accepts_queries():
    assert_read_only("SELECT 1")
    assert_read_only("WITH a AS (SELECT 1) SELECT * FROM a")
    # 注释与字符串字面量里的关键字不应误判
    assert_read_only("-- drop table x\nSELECT 'insert into'")
    assert_read_only("SELECT 1 /* delete from t */")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "UPDATE t SET a = 1",
        "SELECT 1; DROP TABLE t",
        "CREATE TABLE t (a int)",
    ],
)
def test_assert_read_only_rejects_writes(sql):
    with pytest.raises(StarRocksError):
        assert_read_only(sql)


async def test_engine_raises_when_starrocks_unconfigured():
    engine = MetricFlowEngine(layer, StarRocksClient(Settings().starrocks))
    with pytest.raises(StarRocksError):
        await engine.query({"metrics": ["GMV"], "group_by": ["统计日期"]})


# ---- CLI 参数解析 ----


def test_cli_parse_query():
    from dmf_agent.semantic.__main__ import parse_query

    q = parse_query(
        "metric:GMV,日活 group_by:统计日期,渠道 filters:渠道 in APP,小程序 "
        "time:2026-08-01~2026-08-07:day pop:yoy"
    )
    assert q["metrics"] == ["GMV", "日活"]
    assert q["group_by"] == ["统计日期", "渠道"]
    assert q["filters"] == [
        {"field": "渠道", "operator": "in", "values": ["APP", "小程序"]}
    ]
    assert q["time_range"] == {
        "start": "2026-08-01",
        "end": "2026-08-07",
        "granularity": "day",
    }
    assert q["period_comparison"] is True and q["period_comparison_type"] == "yoy"


def test_cli_parse_query_granularity_and_defaults():
    """显式时间粒度必须生效（不能退化为默认 day）；缺省时回退 day。"""
    from dmf_agent.semantic.__main__ import parse_query

    q = parse_query("metric:GMV time:2026-01-01~2026-09-30:month")
    assert q["time_range"]["granularity"] == "month"

    q = parse_query("metric:GMV time:2026-01-01~2026-09-30")
    assert q["time_range"] == {
        "start": "2026-01-01",
        "end": "2026-09-30",
        "granularity": "day",
    }


# ---- API 集成 ----


def test_api_semantic_metrics_and_compile():
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/v1/semantic/metrics")
        assert r.status_code == 200
        metrics = {m["name"]: m for m in r.json()}
        assert "GMV" in metrics and "客单价" in metrics
        assert metrics["GMV"]["dimensions"]  # 指标带可用维度

        r2 = client.get("/api/v1/semantic/dimensions")
        assert r2.status_code == 200
        assert any(d["name"] == "渠道" for d in r2.json())

        # 只编译不执行：应返回 SQL 与输出列
        r3 = client.post(
            "/api/v1/semantic/compile",
            json={
                "metrics": ["成交额"],
                "group_by": ["统计日期", "渠道"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-07", "granularity": "day"},
            },
        )
        assert r3.status_code == 200
        body = r3.json()
        assert "FROM `dws_trade_day`" in body["sql"]
        assert body["columns"] == ["date", "渠道", "GMV"]

        # 未知指标 -> 400
        r4 = client.post("/api/v1/semantic/compile", json={"metrics": ["未知"]})
        assert r4.status_code == 400
        assert "未收录" in r4.json()["detail"]

        # 测试环境未配置 StarRocks -> 502（明确报错，不返回假数据）
        r5 = client.post("/api/v1/semantic/query", json={"metrics": ["GMV"]})
        assert r5.status_code == 502
        assert "StarRocks" in r5.json()["detail"]


# ---- 银行零售存款 / 贷款指标（原子 / 衍生 / 派生）----

#: 10 个银行零售指标 -> 指标类型（原子 simple / 衍生 derived / 派生 ratio）
RETAIL_METRICS = {
    "零售存款余额": "simple",
    "零售贷款余额": "simple",
    "存款利息支出": "simple",
    "贷款利息收入": "simple",
    "不良贷款余额": "simple",
    "贷款客户数": "simple",
    "净利息收入": "derived",
    "户均贷款": "derived",
    "存贷比": "ratio",
    "不良贷款率": "ratio",
}


def test_retail_metrics_cover_atomic_derived_ratio():
    """10 个银行指标齐备，且原子 / 衍生 / 派生三类都有覆盖。"""
    got = {m.name: m.type for m in layer.list_metrics() if m.name in RETAIL_METRICS}
    assert got == RETAIL_METRICS
    assert set(got.values()) == {"simple", "derived", "ratio"}


def test_retail_models_tables_and_aligned_dimensions():
    """存款域 / 贷款域各挂一张物理表，且维度完全一致（跨表指标才能任意分组）。"""
    dep = layer.manifest.model("retail_deposit_day")
    loan = layer.manifest.model("retail_loan_day")
    assert dep.table == "dws_retail_deposit_day"
    assert loan.table == "dws_retail_loan_day"
    expected_dims = ["统计日期", "机构", "零售渠道", "产品类型", "客户类型", "客户等级"]
    assert [d.name for d in dep.dimensions] == expected_dims
    assert [d.name for d in loan.dimensions] == expected_dims
    # 跨表指标的可分组维度 = 两域维度交集 = 全部维度
    assert layer.supported_dimensions(layer.resolve_metric("存贷比")) == expected_dims


def test_retail_alias_resolution():
    assert layer.resolve_metric("存款余额").name == "零售存款余额"
    assert layer.resolve_metric("贷款余额").name == "零售贷款余额"
    assert layer.resolve_metric("不良率").name == "不良贷款率"
    assert layer.resolve_metric("户均贷款余额").name == "户均贷款"


def test_retail_base_measures_expansion():
    assert layer.base_measures("零售存款余额") == ["retail_deposit_balance"]
    assert layer.base_measures("存贷比") == ["retail_loan_balance", "retail_deposit_balance"]
    assert layer.base_measures("不良贷款率") == ["retail_npl_balance", "retail_loan_balance"]
    assert layer.base_measures("净利息收入") == [
        "retail_loan_interest",
        "retail_deposit_interest",
    ]
    assert layer.base_measures("户均贷款") == ["retail_loan_balance", "retail_loan_customer"]


def test_retail_customer_count_measure_is_sum_not_max():
    """回归：客户数是"户数"口径，跨机构加总必须 SUM。

    早期误声明为 MAX，会让"当日全行客户数"退化成单个维度组合的最大值，
    实测使户均贷款从 24.6 万放大到 528 万（约 21 倍）。
    """
    for measure in ("retail_deposit_customer", "retail_loan_customer"):
        owner = layer.manifest.measure_owner(measure)
        assert owner is not None
        assert owner.measure(measure).agg == "sum"


def test_retail_domain_isolated_from_trade_domain():
    """交易域指标不支持银行域维度，反之亦然（跨域误配必须被拦住，而不是静默出错）。"""
    assert "产品类型" not in layer.supported_dimensions(layer.resolve_metric("GMV"))
    problems = layer.validate_elements(
        {"metrics": ["GMV"], "dimensions": ["产品类型"], "time_range": {"granularity": "day"}}
    )
    assert any("所在数据域支持" in p for p in problems)
    assert (
        layer.validate_elements(
            {
                "metrics": ["存贷比"],
                "dimensions": ["机构", "产品类型"],
                "time_range": {"granularity": "day"},
            }
        )
        == []
    )


def test_compile_retail_cross_domain_derived_and_ratio():
    """跨存款/贷款两域的衍生 + 派生指标：两表 JOIN 后在最外层求公式。"""
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "净利息收入"}, {"name": "存贷比"}],
                "dimensions": ["统计日期", "机构"],
                "time_range": {"start": "2026-09-14", "end": "2026-09-14", "granularity": "day"},
            },
            layer,
        )
    )
    sql = compiled.sql
    assert "FROM `dws_retail_loan_day` AS t" in sql
    assert "FROM `dws_retail_deposit_day` AS t" in sql
    assert "FULL OUTER JOIN c_retail_deposit_day AS b ON" in sql
    # derived：贷款利息收入 - 存款利息支出
    assert "- (c.`m_retail_deposit_interest_sum`)" in sql
    # ratio：贷款余额 / 存款余额，*100 输出百分数
    assert "/ NULLIF((c.`m_retail_deposit_balance_sum`), 0)) * 100" in sql
    assert compiled.models == ["retail_loan_day", "retail_deposit_day"]
    assert compiled.columns == ["date", "机构", "净利息收入", "存贷比"]


def test_compile_retail_derived_uses_count_measure():
    compiled = compiler.compile(MetricQuery.from_dsl({"metrics": [{"name": "户均贷款"}]}, layer))
    assert "`m_retail_loan_balance_sum`" in compiled.sql
    assert "`m_retail_loan_customer_sum`" in compiled.sql


def test_retail_supports_granularity_and_period_comparison():
    """派生指标同样支持时间限定（月粒度）与同比，产出 __前期/__增长值/__增长率。"""
    metric = layer.resolve_metric("零售存款余额")
    assert layer.validate_metric_support(metric, granularity="month", pop_type="yoy") == []
    compiled = compiler.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "存贷比"}],
                "dimensions": ["统计日期"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-14", "granularity": "month"},
                "period_comparison": True,
                "period_comparison_type": "yoy",
            },
            layer,
        )
    )
    assert {"存贷比", "存贷比__前期", "存贷比__增长值", "存贷比__增长率"}.issubset(
        set(compiled.columns)
    )


def test_api_semantic_metrics_includes_retail():
    from fastapi.testclient import TestClient

    from dmf_agent.api.app import app

    with TestClient(app) as client:
        r = client.get("/api/v1/semantic/metrics")
        assert r.status_code == 200
        metrics = {m["name"]: m for m in r.json()}
        assert set(RETAIL_METRICS).issubset(set(metrics))
        assert metrics["存贷比"]["type"] == "ratio"
        assert "机构" in metrics["存贷比"]["dimensions"]

        r2 = client.get("/api/v1/semantic/dimensions")
        names = {d["name"] for d in r2.json()}
        assert {"机构", "零售渠道", "产品类型", "客户类型", "客户等级"}.issubset(names)

        # 银行域指标可正常编译（不执行）
        r3 = client.post(
            "/api/v1/semantic/compile",
            json={
                "metrics": ["不良贷款率"],
                "group_by": ["统计日期", "机构"],
                "time_range": {"start": "2026-09-14", "end": "2026-09-14", "granularity": "day"},
            },
        )
        assert r3.status_code == 200
        assert "FROM `dws_retail_loan_day`" in r3.json()["sql"]


def test_advanced_same_metric_items_are_merged():
    """回归：同名指标的多个二次分析项必须合并，不能后者覆盖前者。

    历史实现 `ops[name] = op` 会让 "agg=avg" 被 "proportion=true" 整条替换，
    聚合覆盖静默丢失（前端 / LLM 很容易拆成两个 item 生成）。
    """
    query = MetricQuery.from_dsl(
        {
            "metrics": ["零售存款余额"],
            "advanced": {
                "metric_result": [
                    {"metric_name": "零售存款余额", "agg": "avg"},
                    {"metric_name": "零售存款余额", "proportion": True},
                    {"metric_name": "零售存款余额", "orderby": "desc", "limit": "3"},
                ]
            },
        },
        layer,
    )
    op = query.metric_ops["零售存款余额"]
    assert op.agg_override == "avg"
    assert op.proportion is True
    assert op.rank is True and op.rank_order == "desc" and op.rank_limit == 3


def test_advanced_pure_default_agg_produces_no_op():
    """agg=sum 是默认值，不应产生算子（避免无谓的 SQL 分支）。"""
    query = MetricQuery.from_dsl(
        {
            "metrics": ["零售存款余额"],
            "advanced": {"metric_result": [{"metric_name": "零售存款余额", "agg": "sum"}]},
        },
        layer,
    )
    assert query.metric_ops == {}


def _last_agg_layer() -> SemanticLayer:
    """构造一个带 `last`/`first` 聚合 measure 的最小语义层（期末/期初余额场景）。"""
    raw = {
        "semantic_models": [
            {
                "name": "bal_day",
                "table": "dws_balance_day",
                "dimensions": [
                    {"name": "统计日期", "type": "time", "expr": "stat_date"},
                    {"name": "机构", "type": "categorical", "expr": "org_name"},
                ],
                "measures": [
                    {"name": "end_balance", "agg": "last", "expr": "end_balance"},
                    {"name": "start_balance", "agg": "first", "expr": "start_balance"},
                ],
            }
        ],
        "metrics": [
            {"name": "期末余额", "type": "simple", "type_params": {"measure": "end_balance"}},
            {"name": "期初余额", "type": "simple", "type_params": {"measure": "start_balance"}},
        ],
    }
    return SemanticLayer.from_manifest_dict(raw)


def test_last_first_agg_compiles_to_arg_max_min():
    """S7：`last`/`first` 编译为按时间维度取最值对应的度量值（期间末/初值）。"""
    layer2 = _last_agg_layer()
    # 校验器应接受 last/first 聚合（不再报"聚合方式不支持"）
    assert layer2.manifest.validate() == []

    comp = MetricFlowSqlCompiler(layer2, Settings().starrocks)
    sql = comp.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "期末余额"}, {"name": "期初余额"}],
                "dimensions": ["机构"],
                "time_range": {"start": "2026-01-01", "end": "2026-03-31", "granularity": "month"},
            },
            layer2,
        )
    ).sql
    assert "ARG_MAX(t.end_balance, t.stat_date)" in sql
    assert "ARG_MIN(t.start_balance, t.stat_date)" in sql


def test_last_agg_mysql_dialect_emulates_arg_max():
    """mysql 方言下 last/first 用 GROUP_CONCAT 模拟时点取值（MySQL 无 MAX_BY/ARG_MAX）。"""
    layer2 = _last_agg_layer()
    from dmf_agent.config.settings import StarRocksSettings

    mysql_settings = StarRocksSettings(dialect="mysql")
    comp = MetricFlowSqlCompiler(layer2, mysql_settings)
    sql = comp.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "期末余额"}],
                "dimensions": ["机构"],
                "time_range": {"start": "2026-01-01", "end": "2026-03-31", "granularity": "month"},
            },
            layer2,
        )
    ).sql
    assert (
        "CAST(SUBSTRING_INDEX(GROUP_CONCAT(t.end_balance ORDER BY t.stat_date DESC), ',', 1)"
        " AS DECIMAL(65,10))" in sql
    )


def test_last_agg_all_dialects():
    """四种方言的时点取值（last）与标识符引用各按其语法生成。"""
    layer2 = _last_agg_layer()
    from dmf_agent.config.settings import StarRocksSettings
    from dmf_agent.semantic.dialects import get_dialect

    cases = {
        "starrocks": "ARG_MAX(t.end_balance, t.stat_date)",
        "mysql": (
            "CAST(SUBSTRING_INDEX(GROUP_CONCAT(t.end_balance ORDER BY t.stat_date DESC), ',', 1)"
            " AS DECIMAL(65,10))"
        ),
        "postgresql": '(ARRAY_AGG(t.end_balance ORDER BY t.stat_date DESC))[1]',
        "oracle": "MAX(t.end_balance) KEEP (DENSE_RANK LAST ORDER BY t.stat_date)",
    }
    for name, expect in cases.items():
        comp = MetricFlowSqlCompiler(layer2, StarRocksSettings(dialect=name))
        assert comp.dialect == name
        sql = comp.compile(
            MetricQuery.from_dsl(
                {
                    "metrics": [{"name": "期末余额"}],
                    "dimensions": ["机构"],
                    "time_range": {"start": "2026-01-01", "end": "2026-03-31", "granularity": "month"},
                },
                layer2,
            )
        ).sql
        assert expect in sql, f"方言 {name} 未生成预期的时点取值表达式"
        # 时点取值属于聚合，不依赖 FULL JOIN；引用符按方言
        d = get_dialect(name)
        assert d.ident("机构") in sql or "`机构`" in sql


def test_dialect_quoting_and_date_trunc():
    """方言差异逐项：标识符引用 / date_trunc / 月份平移 / FULL JOIN 支持。"""
    from dmf_agent.semantic.dialects import get_dialect

    sr = get_dialect("starrocks")
    my = get_dialect("mysql")
    pg = get_dialect("postgresql")
    ora = get_dialect("oracle")

    # 标识符
    assert sr.ident("GMV") == "`GMV`"
    assert pg.ident("GMV") == '"GMV"'
    assert ora.ident('a"b') == '"a""b"'
    # date_trunc
    assert sr.date_trunc("month", "x") == "date_trunc('month', x)"
    assert pg.date_trunc("month", "x") == "date_trunc('month', x)"
    assert "STR_TO_DATE(DATE_FORMAT(x, '%Y-%m-01')" in my.date_trunc("month", "x")
    assert ora.date_trunc("month", "x") == "TRUNC(x, 'MM')"
    assert ora.date_trunc("week", "x") == "TRUNC(x, 'IW')"
    # 月份平移（同比 12 个月）
    assert sr.shift_time("x", {"months": -12}) == "add_months(x, -12)"
    assert my.shift_time("x", {"months": -12}) == "DATE_ADD(x, INTERVAL -12 MONTH)"
    assert pg.shift_time("x", {"months": -12}) == "(x + INTERVAL '-12 months')"
    assert ora.shift_time("x", {"months": -12}) == "ADD_MONTHS(x, -12)"
    # 日平移（环比按天粒度）
    assert my.shift_time("x", {"days": -1}) == "DATE_ADD(x, INTERVAL -1 DAY)"
    assert pg.shift_time("x", {"days": -1}) == "(x + INTERVAL '-1 days')"
    assert ora.shift_time("x", {"days": -1}) == "(x + -1)"
    # FULL JOIN 支持
    assert sr.supports_full_outer_join and pg.supports_full_outer_join and ora.supports_full_outer_join
    assert not my.supports_full_outer_join


def test_unknown_dialect_rejected():
    from dmf_agent.semantic.dialects import get_dialect

    with pytest.raises(ValueError):
        get_dialect("sqlite")


# ---- S6 两阶段聚合（先按天 SUM、再按目标粒度 AVG）----


def _two_stage_layer() -> SemanticLayer:
    """构造一个带两阶段指标的最小语义层（月日均存款余额场景）。"""
    raw = {
        "semantic_models": [
            {
                "name": "deposit_day",
                "table": "dws_deposit_day",
                "dimensions": [
                    {"name": "统计日期", "type": "time", "expr": "stat_date"},
                    {"name": "机构", "type": "categorical", "expr": "org_name"},
                ],
                "measures": [
                    {"name": "balance", "agg": "sum", "expr": "acct_balance"},
                ],
            }
        ],
        "metrics": [
            {"name": "存款余额", "type": "simple", "type_params": {"measure": "balance"}},
            {
                "name": "日均存款余额",
                "type": "simple",
                "type_params": {"measure": "balance"},
                "two_stage": {"inner": "sum", "outer": "avg"},
            },
        ],
    }
    return SemanticLayer.from_manifest_dict(raw)


def test_two_stage_metric_compiles_to_nested_avg_of_daily_sum():
    """S6：月日均余额 = 按天 SUM 余额 -> 按月 AVG 每日余额（非底表行均值）。"""
    layer2 = _two_stage_layer()
    comp = MetricFlowSqlCompiler(layer2, Settings().starrocks)
    sql = comp.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "日均存款余额"}],
                "dimensions": ["统计日期"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-30", "granularity": "month"},
            },
            layer2,
        )
    ).sql
    # 日内子查询：按天 SUM 余额（两阶段列用 __ts 后缀区分）
    assert "SUM(t.acct_balance) AS `m_balance_sum__ts__day`" in sql
    # 外层：对每日余额再求均值（目标粒度）
    assert "AVG(sub.`m_balance_sum__ts__day`) AS `m_balance_sum__ts`" in sql
    # 最终输出引用两阶段列
    assert "ROUND(c.`m_balance_sum__ts`, 4) AS `日均存款余额`" in sql


def test_two_stage_and_plain_same_measure_coexist():
    """同一 measure 既出单阶段总量、又出两阶段日均：两列必须独立（__ts 后缀）。"""
    layer2 = _two_stage_layer()
    comp = MetricFlowSqlCompiler(layer2, Settings().starrocks)
    sql = comp.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "存款余额"}, {"name": "日均存款余额"}],
                "dimensions": ["统计日期"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-30", "granularity": "month"},
            },
            layer2,
        )
    ).sql
    # 单阶段总量：日内 SUM 后目标粒度再 SUM（= 总量）
    assert "SUM(sub.`m_balance_sum__day`) AS `m_balance_sum`" in sql
    # 两阶段日均：日内 SUM 后目标粒度 AVG（内层列带 __ts 后缀）
    assert "AVG(sub.`m_balance_sum__ts__day`) AS `m_balance_sum__ts`" in sql
    assert "ROUND(c.`m_balance_sum`, 4) AS `存款余额`" in sql
    assert "ROUND(c.`m_balance_sum__ts`, 4) AS `日均存款余额`" in sql


def test_two_stage_metric_without_inner_defaults_to_measure_agg():
    """two_stage 省略 inner 时，内层聚合回落到 measure.agg（sum）。"""
    layer2 = _two_stage_layer()
    raw = layer2.manifest.to_dict()
    # 改写：去掉 inner，仅保留 outer
    for m in raw["metrics"]:
        if m["name"] == "日均存款余额":
            m["two_stage"] = {"outer": "avg"}
    layer3 = SemanticLayer.from_manifest_dict(raw)
    comp = MetricFlowSqlCompiler(layer3, Settings().starrocks)
    sql = comp.compile(
        MetricQuery.from_dsl(
            {
                "metrics": [{"name": "日均存款余额"}],
                "dimensions": ["统计日期"],
                "time_range": {"start": "2026-09-01", "end": "2026-09-30", "granularity": "month"},
            },
            layer3,
        )
    ).sql
    assert "SUM(t.acct_balance) AS `m_balance_sum__ts__day`" in sql
    assert "AVG(sub.`m_balance_sum__ts__day`) AS `m_balance_sum__ts`" in sql


def test_two_stage_validation_rejects_non_simple():
    """two_stage 只能用于 simple 指标，ratio/derived 应被清单校验拒绝。"""
    raw = {
        "semantic_models": [
            {
                "name": "m1",
                "table": "t1",
                "dimensions": [{"name": "统计日期", "type": "time", "expr": "d"}],
                "measures": [{"name": "x", "agg": "sum", "expr": "x"}],
            }
        ],
        "metrics": [
            {
                "name": "bad",
                "type": "ratio",
                "type_params": {"numerator": "x", "denominator": "x"},
                "two_stage": {"outer": "avg"},
            }
        ],
    }
    with pytest.raises(Exception):
        SemanticLayer.from_manifest_dict(raw)


def test_two_stage_validation_rejects_bad_outer():
    """two_stage.outer 必须是受支持的聚合方式。"""
    raw = {
        "semantic_models": [
            {
                "name": "m1",
                "table": "t1",
                "dimensions": [{"name": "统计日期", "type": "time", "expr": "d"}],
                "measures": [{"name": "x", "agg": "sum", "expr": "x"}],
            }
        ],
        "metrics": [
            {
                "name": "bad",
                "type": "simple",
                "type_params": {"measure": "x"},
                "two_stage": {"outer": "bogus"},
            }
        ],
    }
    with pytest.raises(Exception):
        SemanticLayer.from_manifest_dict(raw)


