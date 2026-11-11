"""Phase 4c 归因核心 + 节点测试（纯 Python，无外部依赖）。"""

from dmf_agent.diagnosis.core import (
    contribution_top,
    dim_attribution,
    global_summary,
    metric_change,
    rows_to_dicts,
    smart_summary,
)
from dmf_agent.agents.metric_agent.nodes.diagnose import _diagnose

# 两组时间点、两个维度值：A 上升 +20，B 下降 -10，整体 +10
QR = {
    "columns": ["统计时间", "门店", "GMV"],
    "rows": [
        ["2026-08-01", "A", 100],
        ["2026-08-01", "B", 50],
        ["2026-08-07", "A", 120],
        ["2026-08-07", "B", 40],
    ],
}


# ---- rows_to_dicts ----


def test_rows_to_dicts():
    rows = rows_to_dicts(QR)
    assert rows[0] == {"统计时间": "2026-08-01", "门店": "A", "GMV": 100}
    assert len(rows) == 4


# ---- metric_change（主指标概况） ----


def test_metric_change():
    change = metric_change(rows_to_dicts(QR), "统计时间", "GMV")
    assert change["metrics_value"] == 150
    assert change["compare_metrics_value"] == 160
    assert change["change_value"] == 10
    assert change["begin_time"] == "2026-08-01"
    assert change["end_time"] == "2026-08-07"


def test_metric_change_single_time_none():
    rows = [{"统计时间": "2026-08-01", "GMV": 100}]
    assert metric_change(rows, "统计时间", "GMV") is None


def test_metric_change_ignores_rows_without_value():
    rows = [
        {"统计时间": "2026-08-01", "GMV": 100},
        {"统计时间": "2026-08-02", "GMV": None},
        {"统计时间": "2026-08-03", "GMV": 120},
    ]
    change = metric_change(rows, "统计时间", "GMV")
    assert change["begin_time"] == "2026-08-01"
    assert change["end_time"] == "2026-08-03"


# ---- dim_attribution（维度值归因） ----


def test_dim_attribution():
    results = dim_attribution(rows_to_dicts(QR), "统计时间", "门店", "GMV")
    # 按 |变化量| 降序：A(20) 在前，B(10) 在后
    assert [r["dim_value"] for r in results] == ["A", "B"]
    a, b = results[0], results[1]
    assert a["change_value"] == 20
    assert b["change_value"] == -10
    # effect_rate 占比和为 1
    assert round(sum(r["effect_rate"] for r in results), 4) == 1.0
    assert a["effect_rate"] > b["effect_rate"]


def test_dim_attribution_single_time_empty():
    rows = [{"统计时间": "2026-08-01", "门店": "A", "GMV": 100}]
    assert dim_attribution(rows, "统计时间", "门店", "GMV") == []


# ---- contribution_top（同向/反向 TOP） ----


def test_contribution_top_rising_metric():
    results = dim_attribution(rows_to_dicts(QR), "统计时间", "门店", "GMV")
    top = contribution_top(results, metric_change_value=10)
    assert [r["dim_value"] for r in top["same"]] == ["A"]
    assert [r["dim_value"] for r in top["opposite"]] == ["B"]


def test_contribution_top_falling_metric_swaps():
    results = dim_attribution(rows_to_dicts(QR), "统计时间", "门店", "GMV")
    top = contribution_top(results, metric_change_value=-10)
    # 整体下降时，负向变化值(B)转为同向
    assert [r["dim_value"] for r in top["same"]] == ["B"]
    assert [r["dim_value"] for r in top["opposite"]] == ["A"]


# ---- 规则总结 ----


def test_global_summary():
    change = metric_change(rows_to_dicts(QR), "统计时间", "GMV")
    text = global_summary(change)
    assert "GMV在这期间从150.0变为160.0" in text
    assert "上升了10.0" in text


def test_smart_summary_rising():
    results = dim_attribution(rows_to_dicts(QR), "统计时间", "门店", "GMV")
    change = metric_change(rows_to_dicts(QR), "统计时间", "GMV")
    top = contribution_top(results, change["change_value"])
    text = smart_summary(change, top["same"], top["opposite"], global_summary(change))
    assert "上升" in text
    assert "门店是A" in text
    assert "门店是B" in text


def test_smart_summary_zero_change():
    rows = [
        {"统计时间": "2026-08-01", "门店": "A", "GMV": 100},
        {"统计时间": "2026-08-07", "门店": "A", "GMV": 100},
    ]
    change = metric_change(rows, "统计时间", "GMV")
    text = smart_summary(change, [], [], global_summary(change))
    assert "没有发生变化" in text


# ---- _diagnose 节点计算 ----


def test_diagnose_full():
    dsl = {"metrics": [{"code": "67", "name": "GMV"}], "dimensions": ["门店"]}
    result = _diagnose(QR, dsl)
    assert result["ok"] is True
    assert result["metric"] == "GMV"
    assert result["change"]["change_value"] == 10
    assert result["dimensions"]["门店"]
    assert "上升" in result["smart_summary"]


def test_diagnose_action_hints():
    # FR3：基于确定性贡献TOP生成行动建议线索（无知识库依赖）
    dsl = {"metrics": [{"code": "67", "name": "GMV"}], "dimensions": ["门店"]}
    result = _diagnose(QR, dsl)
    hints = result.get("action_hints") or []
    assert hints  # 既有同向又有反向贡献，应给出排查/复用建议
    joined = " ".join(hints)
    assert "门店" in joined
    assert "反向贡献" in joined or "同向贡献" in joined


def test_diagnose_no_data():
    result = _diagnose({"columns": [], "rows": []}, {"metrics": [{"code": "g", "name": "GMV"}]})
    assert result["ok"] is False


def test_diagnose_single_timepoint():
    qr = {
        "columns": ["统计时间", "GMV"],
        "rows": [["2026-08-01", 100]],
    }
    dsl = {"metrics": [{"code": "g", "name": "GMV"}], "dimensions": []}
    result = _diagnose(qr, dsl)
    assert result["ok"] is False
    assert "不足两个" in result["message"]


def test_diagnose_dim_cols_missing_graceful():
    # 维度列不在取数结果中 -> 该维度跳过，主指标归因仍可用
    qr = {
        "columns": ["统计时间", "GMV"],
        "rows": [["2026-08-01", 100], ["2026-08-07", 120]],
    }
    dsl = {"metrics": [{"code": "g", "name": "GMV"}], "dimensions": ["门店"]}
    result = _diagnose(qr, dsl)
    assert result["ok"] is True
    assert result["dimensions"] == {}


# ---- 主图集成：归因意图走 diagnose_data 节点 ----


async def test_graph_diagnosis_runs_diagnose_node():
    from langchain_core.messages import HumanMessage

    from dmf_agent.agents.metric_agent import build_metric_agent_graph
    from fakes import make_deps

    graph = build_metric_agent_graph(make_deps())
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="近7天GMV为什么下降了，帮我做个归因分析")],
            "session_id": "d1",
            "user_id": "tester",
        }
    )
    assert result["intent"] == "diagnosis"
    assert result["diagnosis"]["ok"] is True
    assert result["diagnosis"]["metric"] == "GMV"
    assert "归因" in result["final_answer"]
