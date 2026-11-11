"""Phase 4a/4b 二次分析判断 + 高级分析测试（无 LLM，取数用假语义引擎）。"""

from langchain_core.messages import HumanMessage

from dmf_agent.agents.metric_agent.nodes.analyze_data import (
    _complete_metric_result,
    _extract_limit,
    _generate_advanced_rules,
    _needs_secondary_analysis,
)
from dmf_agent.config.settings import HMSettings, Settings
from dmf_agent.integrations.metric_platform.gain_api import _build_advanced_config, build_gain_request

HM_SETTINGS = Settings(hm=HMSettings(api_base="http://hm:8080"))
DIM_NAME2ID = {"门店": 38}

DSL = {
    "metrics": [{"code": "67", "name": "国内日销量"}],
    "dimensions": ["门店"],
    "filters": [],
    "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
    "order_by": None,
    "period_comparison": False,
    "period_comparison_type": None,
    "advanced": {
        "metric_result": [
            {
                "metric_name": "国内日销量",
                "agg": "sum",
                "filter_operator": "",
                "condition_value": "",
                "orderby": "desc",
                "limit": "3",
                "proportion": False,
            }
        ],
        "groupby_filed": ["门店", "day"],
        "ranking_method": "global",
    },
}


# ---- 二次分析判断 ----


def test_needs_secondary_keyword_hit():
    state = {"messages": [HumanMessage(content="各门店的平均销售额")], "skills": []}
    assert _needs_secondary_analysis(state) is True


def test_needs_secondary_skill_hit():
    state = {
        "messages": [HumanMessage(content="帮我算一下")],
        "skills": ["complex_calculation"],
    }
    assert _needs_secondary_analysis(state) is True


def test_needs_secondary_plain_query():
    state = {"messages": [HumanMessage(content="查一下近7天的GMV趋势")], "skills": []}
    assert _needs_secondary_analysis(state) is False


def test_needs_secondary_pure_ranking_handled_by_first_fetch():
    # FR1：纯排名 / TopN / 最高最低 / 求和已由首次取数的 ORDER BY+LIMIT 与默认 sum 满足
    for text in ("GMV最高的门店", "取GMV前10的门店", "top5渠道", "各门店GMV总和", "GMV最低的3个门店"):
        state = {"messages": [HumanMessage(content=text)], "skills": []}
        assert _needs_secondary_analysis(state) is False, text


# ---- top-n 解析 ----


def test_extract_limit():
    assert _extract_limit("取前3的门店") == 3
    assert _extract_limit("top5") == 5
    assert _extract_limit("第2的门店") == 2
    assert _extract_limit("排名前3名") == 3
    assert _extract_limit("各门店的GMV") is None


# ---- mock 二次分析规则 ----


def test_rules_groupby_and_topn():
    elements = {"metrics": ["GMV"], "dimensions": ["门店"]}
    config = _generate_advanced_rules("查询各门店的GMV总和，按门店分组，取前3的门店", elements)
    item = config["metric_result"][0]
    assert item["agg"] == "sum"
    assert item["orderby"] == "desc"
    assert item["limit"] == "3"
    assert config["groupby_filed"] == ["门店"]
    assert config["ranking_method"] == "global"


def test_rules_avg_and_proportion():
    elements = {"metrics": ["销售额"], "dimensions": []}
    config = _generate_advanced_rules("所有门店的平均销售额占比", elements)
    assert config["metric_result"][0]["agg"] == "avg"
    assert config["metric_result"][0]["proportion"] is True


def test_rules_max_min_no_longer_agg_override():
    # FR1：最高/最低是排序（ORDER BY + LIMIT），不再当成 max()/min() 聚合覆盖
    elements = {"metrics": ["GMV"], "dimensions": []}
    top = _generate_advanced_rules("GMV最高的门店", elements)["metric_result"][0]
    bottom = _generate_advanced_rules("GMV最低的门店", elements)["metric_result"][0]
    assert top["agg"] == "sum"
    assert bottom["agg"] == "sum"


def test_rules_no_keyword_default_sum():
    elements = {"metrics": ["GMV"], "dimensions": []}
    config = _generate_advanced_rules("各门店GMV", elements)
    item = config["metric_result"][0]
    assert item["agg"] == "sum"
    assert item["orderby"] == ""
    assert item["limit"] == ""


def test_complete_metric_result_adds_missing():
    config = {
        "metric_result": [{"metric_name": "GMV", "agg": "avg"}],
        "groupby_filed": [],
        "ranking_method": "",
    }
    config = _complete_metric_result(config, ["GMV", "DAU"])
    names = {item["metric_name"] for item in config["metric_result"]}
    assert names == {"GMV", "DAU"}
    dau = next(i for i in config["metric_result"] if i["metric_name"] == "DAU")
    assert dau["agg"] == "sum"


# ---- 二次分析 -> HM 请求体（老 get_hmApi_analysis） ----


def test_advanced_config_metrics_config_list():
    dsl = {**DSL, "metrics": [{"code": "67", "name": "国内日销量"}, {"code": "68", "name": "海外日销量"}],
           "advanced": {
               "metric_result": [
                   {"metric_name": "国内日销量", "agg": "sum", "orderby": "desc", "limit": "3", "proportion": False},
                   {"metric_name": "海外日销量", "agg": "avg", "orderby": "", "limit": "", "proportion": False},
               ],
               "groupby_filed": ["门店", "day"],
               "ranking_method": "global",
           }}
    adv = _build_advanced_config(dsl, DIM_NAME2ID)
    assert adv["analysisDimIds"] == [38]
    assert adv["analysisTimeDimension"] == {"isDimension": True, "dateFormat": ""}
    # 国内日销量：基础 sum 配置 + 排名列 rankFunc
    ids = [cfg["id"] for cfg in adv["metricsConfigList"]]
    assert ids == [67, 67, 68]
    rank = adv["metricsConfigList"][1]
    assert rank["metricAdvanceFunc"]["type"] == "rankFunc"
    assert adv["metricsConfigList"][2]["countWay"] == "avg"
    # 排名截断 havingCondition
    assert adv["havingCondition"]["children"][0]["children"][0]["baseContent"]["content"] == "3"


def test_advanced_config_proportion_and_median():
    dsl = {**DSL, "advanced": {
        "metric_result": [
            {"metric_name": "国内日销量", "agg": "median", "proportion": True},
        ],
        "groupby_filed": [],
        "ranking_method": "",
    }}
    adv = _build_advanced_config(dsl, DIM_NAME2ID)
    assert adv["metricsConfigList"][0]["countWay"] == "percentile_cont"
    # 占比为独立配置项（index 1，带 groupPercentFunc）
    assert adv["metricsConfigList"][1]["metricAdvanceFunc"]["type"] == "groupPercentFunc"
    assert adv["analysisTimeDimension"] == {"isDimension": False, "dateFormat": ""}


def test_advanced_config_empty_without_metric_result():
    assert _build_advanced_config({**DSL, "advanced": {"metric_result": []}}, DIM_NAME2ID) == {}


def test_gain_request_with_advanced_default_order_bys():
    body = build_gain_request(DSL, DIM_NAME2ID, HM_SETTINGS)
    assert body["metricsConfigList"]
    # 默认排序：dt 降序 + 首个指标(67)降序，indexId 指向其基础配置
    assert body["orderBys"] == [
        {"subjectType": "dt", "order": "DESC"},
        {"subjectType": "metrics", "subjectKey": 67, "order": "DESC", "indexId": 1},
    ]


def test_gain_request_without_advanced_no_metrics_config():
    body = build_gain_request({k: v for k, v in DSL.items() if k != "advanced"}, DIM_NAME2ID, HM_SETTINGS)
    assert "metricsConfigList" not in body


# ---- analyze_data 节点集成 ----


async def test_analyze_secondary_analysis_reruns_engine():
    """calc 层（均值 + 排名）二次分析：配置生成后追加一次语义引擎取数。"""
    from dmf_agent.agents.metric_agent.nodes.analyze_data import build_analyze_data_node
    from fakes import make_deps

    deps = make_deps()
    node = build_analyze_data_node(deps)
    state = {
        "messages": [HumanMessage(content="查询各门店的平均GMV，取前3的门店")],
        "query_elements": {
            "metrics": ["GMV"],
            "dimensions": ["门店"],
            "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
        },
        "dsl": {
            "version": "3.0",
            "engine": "metricflow",
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "filters": [],
            "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
        },
        "skills": [],
        "intent": "metric_query",
    }
    result = await node(state)
    analysis = result["analysis"]
    assert analysis["secondary_analysis"] is True
    item = analysis["advanced"]["metric_result"][0]
    assert item["agg"] == "avg"
    assert item["limit"] == "3"
    # 二次取数走同一语义引擎（dsl + advanced 交给编译器转 SQL），非 mock
    assert len(deps.metric_engine.queries) == 1
    assert deps.metric_engine.queries[0]["advanced"]["metric_result"][0]["limit"] == "3"
    assert analysis["advanced_result"] is not None
    assert analysis["advanced_result"]["mock"] is False
    assert analysis["advanced_result"]["row_count"] > 0


async def test_analyze_pure_ranking_served_by_first_fetch():
    """FR1：纯排名/TopN 不再二次取数（首次 ORDER BY+LIMIT 已满足），直接用首次结果做统计。"""
    from dmf_agent.agents.metric_agent.nodes.analyze_data import build_analyze_data_node
    from fakes import make_deps

    deps = make_deps()
    node = build_analyze_data_node(deps)
    state = {
        "messages": [HumanMessage(content="查询各门店GMV总和，按门店分组，取前3的门店")],
        "query_elements": {
            "metrics": ["GMV"],
            "dimensions": ["门店"],
            "order_by": "GMV desc",
            "limit": 3,
            "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
        },
        "dsl": {
            "version": "3.0",
            "engine": "metricflow",
            "metrics": [{"name": "GMV"}],
            "dimensions": ["门店"],
            "order_by": "GMV desc",
            "limit": 3,
            "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
        },
        "query_result": {
            "columns": ["门店", "GMV"],
            "rows": [["A店", 100], ["B店", 200], ["C店", 300]],
        },
        "skills": [],
        "intent": "metric_query",
    }
    result = await node(state)
    analysis = result["analysis"]
    assert analysis["secondary_analysis"] is False
    assert analysis["advanced"] is None
    # analyze 节点不再为纯排名发起任何引擎查询
    assert len(deps.metric_engine.queries) == 0
    assert analysis["stats"]["GMV"]["avg"] == 200


async def test_analyze_plain_no_secondary():
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.analyze_data import build_analyze_data_node

    deps = AgentDeps(settings=Settings(), get_chat_model=lambda: None)
    node = build_analyze_data_node(deps)
    state = {
        "messages": [HumanMessage(content="查一下近7天的GMV趋势")],
        "query_elements": {"metrics": ["GMV"], "dimensions": [], "time_range": None},
        "dsl": {"metrics": [{"name": "GMV"}], "dimensions": []},
        "query_result": {
            "columns": ["date", "GMV"],
            "rows": [["2026-08-01", 100], ["2026-08-02", 200]],
        },
        "skills": [],
        "intent": "metric_query",
    }
    result = await node(state)
    analysis = result["analysis"]
    assert analysis["secondary_analysis"] is False
    assert analysis["advanced"] is None
    assert analysis["stats"]["GMV"]["avg"] == 150
