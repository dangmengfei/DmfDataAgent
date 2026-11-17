"""归因优化 P1 测试（2026-11-12）：期间对比 + 异动前置 + 自动补维度 + 结构变化标注。

覆盖：
- core.period_change / period_dim_attribution / period_global_summary / period_smart_summary
- anomaly.detect_anomaly 四态判定
- diagnose 节点的期间/端点口径切换、自动补取数（成功/失败降级）、时间列缺失的显式报错
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dmf_agent.diagnosis.anomaly import detect_anomaly
from dmf_agent.diagnosis.core import (
    period_change,
    period_dim_attribution,
    period_global_summary,
    period_smart_summary,
    rows_to_dicts,
)

# 6 个时间点 × 2 门店：前半段（上期）A=100/100/100, B=50/50/50
# 后半段（本期）A=150/150/150（涨），B=40/40/40（跌）
QR_PERIOD = {
    "columns": ["统计日期", "门店", "GMV"],
    "rows": [
        ["2026-08-01", "A", 100],
        ["2026-08-01", "B", 50],
        ["2026-08-02", "A", 100],
        ["2026-08-02", "B", 50],
        ["2026-08-03", "A", 100],
        ["2026-08-03", "B", 50],
        ["2026-08-04", "A", 150],
        ["2026-08-04", "B", 40],
        ["2026-08-05", "A", 150],
        ["2026-08-05", "B", 40],
        ["2026-08-06", "A", 150],
        ["2026-08-06", "B", 40],
    ],
}


# ---------------- period_change ----------------


def test_period_change_splits_window_in_half():
    change = period_change(rows_to_dicts(QR_PERIOD), "统计日期", "GMV")
    # 上期总量 = (100+50)*3 = 450；本期总量 = (150+40)*3 = 570
    assert change["compare_metrics_value"] == 450
    assert change["metrics_value"] == 570
    assert change["change_value"] == 120
    assert change["compare_mode"] == "period"
    period = change["period"]
    assert period["prev_start"] == "2026-08-01" and period["prev_end"] == "2026-08-03"
    assert period["curr_start"] == "2026-08-04" and period["curr_end"] == "2026-08-06"
    assert period["prev_points"] == 3 and period["curr_points"] == 3


def test_period_change_avoids_single_day_noise():
    """端点对比被首日噪声带偏时，期间对比仍给出稳定结论。"""
    # 首日促销峰值 1000，其余稳定 100；末端 100 → 端点对比显示大幅下降，
    # 期间对比把峰值摊薄到上期 3 个时间点，结论更温和（两者均为下降，但幅度不同）。
    rows = [
        {"统计日期": "2026-08-01", "GMV": 1000},
        {"统计日期": "2026-08-02", "GMV": 100},
        {"统计日期": "2026-08-03", "GMV": 100},
        {"统计日期": "2026-08-04", "GMV": 100},
        {"统计日期": "2026-08-05", "GMV": 100},
        {"统计日期": "2026-08-06", "GMV": 100},
    ]
    change = period_change(rows, "统计日期", "GMV")
    # 上期 = 1200（含峰值），本期 = 300
    assert change["compare_metrics_value"] == 1200
    assert change["metrics_value"] == 300
    assert change["change_value"] == -900


def test_period_change_insufficient_points_returns_none():
    rows = [{"统计日期": "2026-08-01", "GMV": 100}]
    assert period_change(rows, "统计日期", "GMV") is None


def test_period_change_prev_zero_rate_none():
    rows = [
        {"统计日期": "2026-08-01", "GMV": 0},
        {"统计日期": "2026-08-02", "GMV": 100},
    ]
    change = period_change(rows, "统计日期", "GMV")
    assert change["change_rate"] is None  # 上期为 0，不编造比率
    assert "不可计算" in period_global_summary(change)


# ---------------- period_dim_attribution ----------------


def test_period_dim_attribution_kept_members():
    results = period_dim_attribution(rows_to_dicts(QR_PERIOD), "统计日期", "门店", "GMV")
    by_value = {r["dim_value"]: r for r in results}
    assert by_value["A"]["change_value"] == 150  # 450 → 150*3=450? A: 上期300 本期450
    assert by_value["A"]["metrics_value"] == 300
    assert by_value["A"]["compare_metrics_value"] == 450
    assert by_value["B"]["change_value"] == -30  # 上期150 本期120
    assert by_value["A"]["presence"] == "kept"
    assert by_value["B"]["presence"] == "kept"
    # 影响占比在 kept 段内归一
    assert round(sum(r["effect_rate"] for r in results), 4) == 1.0


def test_period_dim_attribution_structure_change():
    """成员新增/消失单独分类，不与涨跌混排。"""
    rows = [
        {"统计日期": "2026-08-01", "门店": "A", "GMV": 100},
        {"统计日期": "2026-08-01", "门店": "B", "GMV": 50},
        {"统计日期": "2026-08-02", "门店": "A", "GMV": 100},
        {"统计日期": "2026-08-02", "门店": "B", "GMV": 50},
        # 本期：B 消失，C 新增，A 不变
        {"统计日期": "2026-08-03", "门店": "A", "GMV": 100},
        {"统计日期": "2026-08-03", "门店": "C", "GMV": 80},
        {"统计日期": "2026-08-04", "门店": "A", "GMV": 100},
        {"统计日期": "2026-08-04", "门店": "C", "GMV": 80},
    ]
    results = period_dim_attribution(rows, "统计日期", "门店", "GMV")
    assert [r["dim_value"] for r in results if r["presence"] == "added"] == ["C"]
    assert [r["dim_value"] for r in results if r["presence"] == "removed"] == ["B"]
    # kept（A 无变化）在结果前部，结构变化成员排在后面
    assert results[0]["dim_value"] == "A"
    assert results[0]["presence"] == "kept"


def test_period_dim_attribution_insufficient_points_empty():
    rows = [{"统计日期": "2026-08-01", "门店": "A", "GMV": 100}]
    assert period_dim_attribution(rows, "统计日期", "门店", "GMV") == []


# ---------------- 规则总结（期间口径） ----------------


def test_period_smart_summary_mentions_both_periods_and_structure():
    rows = rows_to_dicts(QR_PERIOD)
    change = period_change(rows, "统计日期", "GMV")
    results = period_dim_attribution(rows, "统计日期", "门店", "GMV")
    kept = [r for r in results if r["presence"] == "kept"]
    text = period_smart_summary(
        change, kept, [], period_global_summary(change), [], []
    )
    assert "对比口径" in text
    assert "上期" in text and "本期" in text
    assert "上升" in text


# ---------------- detect_anomaly ----------------


def test_detect_anomaly_significant():
    # 上期 100×4，本期 200×4：变化 100%，远超日常波动 → significant
    values = [100.0] * 4 + [200.0] * 4
    result = detect_anomaly(values, change_rate=1.0)
    assert result["status"] == "significant"
    assert result["method"] == "3sigma+period-rate"


def test_detect_anomaly_noise_for_tiny_change():
    values = [100.0, 98.0, 102.0, 99.0, 101.0, 100.0, 99.0, 101.0]
    result = detect_anomaly(values, change_rate=0.005)
    assert result["status"] == "noise"
    assert "正常波动" in result["message"]


def test_detect_anomaly_mild_between_thresholds():
    values = [100.0] * 6
    result = detect_anomaly(values, change_rate=0.01)
    assert result["status"] == "mild"
    assert "仅供参考" in result["message"]


def test_detect_anomaly_insufficient_points():
    assert detect_anomaly([100.0], change_rate=0.5)["status"] == "insufficient"
    assert detect_anomaly([], change_rate=None)["status"] == "insufficient"


def test_detect_anomaly_prev_zero_is_structural():
    result = detect_anomaly([0.0, 50.0], change_rate=None)
    assert result["status"] == "significant"
    assert "上期无数据" in result["message"]


def test_detect_anomaly_outliers_listed_for_long_series():
    values = [100.0] * 10 + [500.0]
    result = detect_anomaly(values, change_rate=0.3)
    assert result["outliers"]  # 500 是明显离群点
    assert result["outliers"][0]["value"] == 500.0


# ---------------- diagnose 节点：自动补维度取数 ----------------


def _node_and_state(deps, dsl_dimensions, query_result, message="近7天GMV为什么下降"):
    from dmf_agent.agents.metric_agent.nodes.diagnose import build_diagnose_node

    state = {
        "query_result": query_result,
        "dsl": {
            "metrics": [{"name": "GMV"}],
            "dimensions": dsl_dimensions,
            "time_range": {"start": "2026-08-01", "end": "2026-08-07", "granularity": "day"},
        },
        "harness_state": {},
        "query_rewritten": message,
    }
    return build_diagnose_node(deps), state


def test_diagnose_auto_fetch_dims_when_no_dimensions():
    """DSL 无维度（真实编译器只输出指标列）-> 自动补维度取数并完成维度归因。

    P2 起：显著异动 + 无 LLM 时规则兜底会再发一次下钻取数（过滤到 TOP 维值 × 新维度），
    所以引擎共收到 2 次查询：自动补维度 + 下钻。
    """
    from fakes import make_deps

    deps = make_deps()
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    assert diag["compare_mode"] == "period"
    assert diag["primary_dim"]  # 补取数后拿到业务维度
    assert list((diag.get("dimensions") or {}).keys())  # 维度归因有内容
    assert "统计日期" not in (diag.get("dimensions") or {})  # 时间列不作为归因维度
    assert diag.get("fetched_dsl")
    # 第 1 次：自动补维度（原始 query_result 为预置）
    assert deps.metric_engine.queries[0]["dimensions"] == [
        "统计日期",
        "渠道",
        "地区",
        "品类",
    ]
    # 第 2 次（P2 规则下钻）：过滤到主维度 TOP 维值 × 新候选维度，剥离排序/TopN
    drill = deps.metric_engine.queries[1]
    assert drill["dimensions"] == ["统计日期", "平台"]
    assert drill["filters"] == [
        {"dimension": "渠道", "dimension_value": ["线上"], "filter_operator": "in"}
    ]
    assert "order_by" not in drill and "limit" not in drill


def test_diagnose_auto_fetch_failure_degrades_with_warning():
    """补取数失败 -> 降级（无时间列时明确报缺，不编造结论）。"""
    from fakes import FakeMetricEngine, make_deps

    deps = make_deps()
    deps.metric_engine = FakeMetricEngine(deps.semantic_layer, fail_with="连接失败")
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is False
    assert diag["warnings"]  # 如实说明补取数失败
    assert "缺少时间列" in diag["message"]


def test_diagnose_auto_fetch_disabled_by_settings():
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.auto_fetch_dims = False
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    result = asyncio.run(node(state))
    assert result["diagnosis"]["ok"] is False
    assert "缺少时间列" in result["diagnosis"]["message"]


def test_diagnose_fetch_goes_through_harness_whitelist_and_budget():
    """补取数经 Harness：白名单放行、工具调用计数回写 state（预算可累计）。"""
    from dmf_agent.config.settings import HarnessSettings
    from dmf_agent.harness import HarnessController, build_default_tool_registry
    from fakes import make_deps

    audit_events = []

    async def _record(event):
        audit_events.append(event)

    harness = HarnessController(
        HarnessSettings(enabled=True),
        registry=build_default_tool_registry(),
        auditor=_record,
    )
    deps = make_deps(harness=harness)
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    state["user_permissions"] = ["semantic:query"]
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    # 工具调用计数回写（P2 起自动补维度 + 规则下钻各一次 diagnose_fetch）
    assert result["harness_state"]["tool_calls"] == 2
    assert any(e.get("tool") == "diagnose_fetch" for e in audit_events)


def test_diagnose_fetch_denied_without_permission_degrades():
    """无 semantic:query 权限 -> Harness 拦截，补取数降级、主链路不中断。"""
    from dmf_agent.config.settings import HarnessSettings
    from dmf_agent.harness import HarnessController, build_default_tool_registry
    from fakes import make_deps

    harness = HarnessController(
        HarnessSettings(enabled=True),
        registry=build_default_tool_registry(),
    )
    deps = make_deps(harness=harness)
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    state["user_permissions"] = []  # 缺 semantic:query
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is False  # 无时间列无法归因，如实报缺
    assert diag["warnings"] and "权限" in " ".join(diag["warnings"])


def test_diagnose_fetch_not_registered_is_rejected():
    """diagnose_fetch 未注册（自定义白名单）-> Harness 拒绝，降级不阻断。"""
    from dmf_agent.config.settings import HarnessSettings
    from dmf_agent.harness import HarnessController, ToolRegistry
    from fakes import make_deps

    harness = HarnessController(HarnessSettings(enabled=True), registry=ToolRegistry())
    deps = make_deps(harness=harness)
    node, state = _node_and_state(deps, [], {"columns": ["GMV"], "rows": [[150]]})
    state["user_permissions"] = ["*"]
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["warnings"]
    assert "白名单" in " ".join(diag["warnings"])


def test_diagnose_endpoint_mode_via_settings():
    """settings 显式 endpoint -> 回落旧端点口径（回归开关）。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.compare_mode = "endpoint"
    qr = {
        "columns": ["统计日期", "门店", "GMV"],
        "rows": [
            ["2026-08-01", "A", 100],
            ["2026-08-01", "B", 50],
            ["2026-08-07", "A", 120],
            ["2026-08-07", "B", 40],
        ],
    }
    node, state = _node_and_state(deps, ["门店"], qr)
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["compare_mode"] == "endpoint"
    assert diag["change"]["change_value"] == 10  # (120+40) - (100+50)


def test_diagnose_period_skips_dim_attribution_on_noise():
    """波动不显著 -> 只输出趋势描述，不输出维度拆解。"""
    from fakes import make_deps

    deps = make_deps()
    qr = {
        "columns": ["统计日期", "门店", "GMV"],
        "rows": [
            ["2026-08-01", "A", 100],
            ["2026-08-02", "A", 101],
            ["2026-08-03", "A", 99],
            ["2026-08-04", "A", 100],
            ["2026-08-05", "A", 101],
            ["2026-08-06", "A", 100],
        ],
    }
    node, state = _node_and_state(deps, ["门店"], qr)
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    assert diag["anomaly"]["status"] == "noise"
    assert diag["skipped_dimensions"] == ["门店"]
    assert diag["dimensions"] == {}
    assert diag["action_hints"] == []


def test_diagnose_time_column_not_treated_as_dimension():
    """时间维度在 dsl.dimensions 中声明时不得作为归因下钻维度。"""
    from fakes import make_deps

    deps = make_deps()
    qr = {
        "columns": ["统计日期", "门店", "GMV"],
        "rows": [
            ["2026-08-01", "A", 100],
            ["2026-08-02", "A", 110],
            ["2026-08-03", "A", 120],
            ["2026-08-04", "A", 130],
        ],
    }
    node, state = _node_and_state(deps, ["统计日期", "门店"], qr)
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert "统计日期" not in (diag.get("dimensions") or {})
    assert diag["primary_dim"] == "门店"
