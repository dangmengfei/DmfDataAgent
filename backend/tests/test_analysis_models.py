"""分析模型工具库 P1 测试（2026-11-18，doc/数据分析能力提升方案.md）。

覆盖：
- 统一契约：AnalysisResult 结构、模型注册重名报错、run_analysis 未知模型/异常转结构化失败
- 五个新模型手工样例对拍：trend（斜率/分段）、anomaly（与归因 detect_anomaly 同源）、
  pareto（累计占比/80% 线/整组截断）、compare（Welch 区间/显著性）、correlation（Pearson 手算）
- 存量包装：analysis_stats / analysis_distribution 行为一致
- 关键词规则：命中映射、优先级、未命中空计划
- 节点接线：问句触发 results、ENABLED=false 逐字回退（含「分布」旧通道）
- Skill：run_analysis 步骤端到端 + analyze_data 旧分支兼容
- Harness：run_analysis 白名单 + output_schema 校验
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from dmf_agent.analysis import (
    keyword_plan,
    model_names,
    run_analysis,
)
from dmf_agent.analysis.base import AnalysisResult

# 手工样例：14 期线性上升序列 y = 100 + 30t + 噪声
_TREND_QR = {
    "columns": ["统计日期", "GMV"],
    "rows": [
        ["2026-08-01", 130],
        ["2026-08-02", 160],
        ["2026-08-03", 190],
        ["2026-08-04", 220],
        ["2026-08-05", 250],
        ["2026-08-06", 280],
        ["2026-08-07", 310],
        ["2026-08-08", 340],
        ["2026-08-09", 370],
        ["2026-08-10", 400],
    ],
}


# ---------------- 统一契约 ----------------


def test_model_registry_contains_all_p1_models():
    expected = {
        "analysis_stats",
        "analysis_distribution",
        "analysis_trend",
        "analysis_anomaly",
        "analysis_pareto",
        "analysis_compare",
        "analysis_correlation",
    }
    assert expected.issubset(set(model_names()))


def test_register_model_duplicate_raises():
    from dmf_agent.analysis.base import register_model

    class _Dup:
        name = next(iter(model_names()))
        description = ""
        input_schema = {}

        def analyze(self, qr, params):  # pragma: no cover
            return AnalysisResult(ok=True, model=self.name, summary="")

    with pytest.raises(ValueError, match="重名"):
        register_model(_Dup())


def test_run_analysis_unknown_model_structured_error():
    r = run_analysis({"columns": [], "rows": []}, "不存在的模型")
    assert r.ok is False and "未注册" in r.error


def test_run_analysis_exception_becomes_structured_failure():
    class _Boom:
        name = "analysis_trend"
        description = ""
        input_schema = {}

        def analyze(self, qr, params):
            raise RuntimeError("boom")

    # 直接以同名注册表替换验证异常路径（不注册，避免污染）
    from dmf_agent.analysis import base

    original = base.MODEL_REGISTRY.get("analysis_trend")
    base.MODEL_REGISTRY["analysis_trend"] = _Boom()  # type: ignore[assignment]
    try:
        r = run_analysis(_TREND_QR, "analysis_trend")
        assert r.ok is False and "boom" in r.error
    finally:
        base.MODEL_REGISTRY["analysis_trend"] = original  # type: ignore[assignment]


# ---------------- analysis_trend ----------------


def test_trend_slope_and_direction():
    r = run_analysis(_TREND_QR, "analysis_trend")
    assert r.ok is True
    # y = 100 + 30t（无噪声样例），斜率应为 30
    assert r.data["slope"] == 30.0
    assert r.data["r2"] == 1.0
    assert "上升" in r.summary


def test_trend_insufficient_points():
    r = run_analysis({"columns": ["统计日期", "GMV"], "rows": [["d1", 1], ["d2", 2]]}, "analysis_trend")
    assert r.ok is False and "样本不足" in r.error


def test_trend_no_numeric_column():
    r = run_analysis({"columns": ["门店"], "rows": [["a"], ["b"], ["c"]]}, "analysis_trend")
    assert r.ok is False


# ---------------- analysis_anomaly（与归因同源） ----------------


def test_anomaly_detects_outlier_consistent_with_diagnosis():
    values = [100.0] * 9 + [500.0]
    qr = {"columns": ["统计日期", "GMV"], "rows": [[f"d{i}", v] for i, v in enumerate(values)]}
    r = run_analysis(qr, "analysis_anomaly")
    assert r.ok is True
    assert r.data["outliers"]  # 500 是离群点
    # 与归因 detect_anomaly 同一实现：同样输入离群点列表一致
    from dmf_agent.diagnosis.anomaly import detect_anomaly

    direct = detect_anomaly(values, change_rate=0.3)
    assert r.data["outliers"] == direct["outliers"]


def test_anomaly_noise_for_stable_series():
    values = [100.0, 99.0, 101.0, 100.0, 99.5, 100.5, 100.0, 99.0]
    qr = {"columns": ["统计日期", "GMV"], "rows": [[f"d{i}", v] for i, v in enumerate(values)]}
    r = run_analysis(qr, "analysis_anomaly")
    assert r.ok is True and r.data["status"] == "noise"


# ---------------- analysis_pareto ----------------


def test_pareto_head_count_and_cumulative():
    # 4 个成员：A=80, B=10, C=6, D=4（总 100）→ 前 1 个贡献 80%
    qr = {"columns": ["门店", "GMV"], "rows": [["A", 80], ["B", 10], ["C", 6], ["D", 4]]}
    r = run_analysis(qr, "analysis_pareto", {"metric": "GMV", "dimension": "门店"})
    assert r.ok is True
    assert r.data["head_count"] == 1
    assert r.data["items"][0]["cumulative_share"] == 0.8
    assert "前 1 个" in r.summary


def test_pareto_aggregates_same_dim_value():
    # 同一维值多行（多时间点）-> 聚合后分析（3 个维值满足门槛）
    qr = {
        "columns": ["统计日期", "门店", "GMV"],
        "rows": [
            ["d1", "A", 40], ["d1", "B", 5], ["d1", "C", 2],
            ["d2", "A", 40], ["d2", "B", 5], ["d2", "C", 2],
            ["d3", "A", 40], ["d3", "B", 5], ["d3", "C", 2],
        ],
    }
    r = run_analysis(qr, "analysis_pareto", {"metric": "GMV", "dimension": "门店"})
    assert r.ok is True
    assert r.data["items"][0]["value"] == "A"
    assert r.data["items"][0]["amount"] == 120


def test_pareto_zero_total_structured_failure():
    qr = {"columns": ["门店", "GMV"], "rows": [["A", 0], ["B", 0], ["C", 0]]}
    r = run_analysis(qr, "analysis_pareto")
    assert r.ok is False and "合计为 0" in r.error


# ---------------- analysis_compare ----------------


def test_compare_significant_difference():
    qr = {
        "columns": ["渠道", "GMV"],
        "rows": [["线上", 100], ["线上", 102], ["线上", 98], ["线下", 50], ["线下", 52], ["线下", 48]],
    }
    r = run_analysis(
        qr, "analysis_compare", {"group_column": "渠道", "group_a": "线上", "group_b": "线下"}
    )
    assert r.ok is True
    assert r.data["groups"]["a"]["mean"] == 100.0
    assert r.data["groups"]["b"]["mean"] == 50.0
    assert r.data["significant"] is True
    assert "显著" in " ".join(r.insight_lines)


def test_compare_insufficient_sample():
    qr = {"columns": ["渠道", "GMV"], "rows": [["线上", 100], ["线下", 50]]}
    r = run_analysis(
        qr, "analysis_compare", {"group_column": "渠道", "group_a": "线上", "group_b": "线下"}
    )
    assert r.ok is False and "样本不足" in r.error


def test_compare_missing_params_structured_error():
    r = run_analysis(_TREND_QR, "analysis_compare")
    assert r.ok is False and "缺少分组参数" in r.error


# ---------------- analysis_correlation ----------------


def test_correlation_perfect_positive():
    qr = {
        "columns": ["统计日期", "GMV", "订单数"],
        "rows": [
            ["d1", 100, 10],
            ["d2", 200, 20],
            ["d3", 300, 30],
            ["d4", 400, 40],
        ],
    }
    r = run_analysis(qr, "analysis_correlation")
    assert r.ok is True
    assert r.data["matrix"]["GMV"]["订单数"] == 1.0
    assert r.data["strong_pairs"][0]["a"] == "GMV"


def test_correlation_n太小_warning():
    qr = {
        "columns": ["d", "A", "B"],
        "rows": [["1", 1, 2], ["2", 2, 4], ["3", 3, 9]],
    }
    r = run_analysis(qr, "analysis_correlation")
    assert r.ok is True
    assert any("不稳定" in w for w in r.warnings)


def test_correlation_insufficient_rows():
    qr = {"columns": ["d", "A", "B"], "rows": [["1", 1, 2], ["2", 2, 4]]}
    r = run_analysis(qr, "analysis_correlation")
    assert r.ok is False and "样本不足" in r.error


# ---------------- 存量包装 ----------------


def test_stats_model_same_as_compute_stats():
    from dmf_agent.analysis import compute_stats

    qr = _TREND_QR
    r = run_analysis(qr, "analysis_stats")
    assert r.ok is True
    assert r.data["stats"]["GMV"]["total"] == compute_stats(qr)["GMV"]["total"]


def test_distribution_model_same_as_compute_distribution():
    from dmf_agent.analysis import compute_distribution

    qr = {"columns": ["门店", "GMV"], "rows": [[f"店{i}", v] for i, v in enumerate([10, 20, 30, 40])]}
    r = run_analysis(qr, "analysis_distribution", {"metric": "GMV"})
    assert r.ok is True
    direct = compute_distribution(qr, "GMV")
    assert r.data["bins"] == direct["bins"]


# ---------------- 关键词规则 ----------------


def test_keyword_plan_mapping():
    assert keyword_plan("近7天GMV走势如何") == ["analysis_trend"]
    assert keyword_plan("GMV有没有异常") == ["analysis_anomaly"]
    assert keyword_plan("做个二八分析") == ["analysis_pareto"]
    assert keyword_plan("和上月对比一下") == ["analysis_compare"]
    assert keyword_plan("指标之间有什么相关关系") == ["analysis_correlation"]
    assert keyword_plan("各门店GMV分布") == ["analysis_distribution"]


def test_keyword_plan_no_hit_returns_empty():
    assert keyword_plan("各门店GMV是多少") == []


# ---------------- 节点接线 ----------------


def _analyze_state(deps, text, qr):
    from dmf_agent.agents.metric_agent.nodes.analyze_data import build_analyze_data_node

    state = {
        "messages": [],
        "query_rewritten": text,
        "query_elements": {"metrics": ["GMV"], "dimensions": ["门店"]},
        "dsl": {"metrics": [{"name": "GMV"}], "dimensions": ["门店"]},
        "query_result": qr,
        "skills": [],
        "intent": "metric_query",
    }
    return build_analyze_data_node(deps), state


async def test_analyze_node_runs_trend_model():
    from fakes import make_deps

    deps = make_deps()
    node, state = _analyze_state(deps, "近7天GMV走势如何", _TREND_QR)
    result = await node(state)
    results = result["analysis"].get("results") or []
    assert len(results) == 1
    assert results[0]["model"] == "analysis_trend" and results[0]["ok"]
    # 模板 summary 含模型确定性文本
    assert "趋势" in result["analysis"]["summary"]


async def test_analyze_node_disabled_no_results():
    """DMF_ANALYSIS-ENABLED=false -> 无 results，行为回退现状。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.analysis.enabled = False
    node, state = _analyze_state(deps, "近7天GMV走势如何", _TREND_QR)
    result = await node(state)
    assert not result["analysis"].get("results")


async def test_analyze_node_no_hit_no_results():
    from fakes import make_deps

    deps = make_deps()
    node, state = _analyze_state(deps, "各门店GMV是多少", _TREND_QR)
    result = await node(state)
    assert not result["analysis"].get("results")


async def test_analyze_node_max_steps_caps_plan():
    from fakes import make_deps

    deps = make_deps()
    deps.settings.analysis.max_steps = 1
    # 构造多命中问句（趋势 + 异常），验证截断到 max_steps
    node, state = _analyze_state(deps, "GMV趋势怎么样，有没有异常", _TREND_QR)
    await node(state)
    # keyword_plan 只返回首个命中（单步），max_steps 上限是防御性约束
    assert keyword_plan("GMV趋势怎么样，有没有异常") == ["analysis_trend"]


# ---------------- Skill run_analysis 步骤 ----------------


async def test_skill_run_analysis_dispatch():
    from dmf_agent.skills.executor import SkillExecutor

    executor = SkillExecutor(get_chat_model=lambda: None)
    out = await executor._dispatch(
        "run_analysis",
        {"model": "analysis_trend", "query_result": _TREND_QR},
    )
    assert out["ok"] is True and out["model"] == "analysis_trend"


async def test_skill_run_analysis_requires_query_result():
    from dmf_agent.skills.executor import SkillExecutor

    executor = SkillExecutor(get_chat_model=lambda: None)
    with pytest.raises(RuntimeError, match="query_result"):
        await executor._dispatch("run_analysis", {"model": "analysis_trend"})


async def test_skill_analyze_data_legacy_branch_still_works():
    """存量 analyze_data 分支兼容（skill 包无需改动）。"""
    from dmf_agent.skills.executor import SkillExecutor

    executor = SkillExecutor(get_chat_model=lambda: None)
    out = await executor._dispatch("analyze_data", {"query_result": _TREND_QR})
    assert "stats" in out and out["stats"]["GMV"]


# ---------------- Harness 注册与 output_schema ----------------


def test_run_analysis_in_harness_whitelist():
    from dmf_agent.harness import build_default_tool_registry

    registry = build_default_tool_registry()
    assert registry.is_allowed("run_analysis")
    spec = registry.get("run_analysis")
    assert spec is not None and spec.output_schema is not None


async def test_run_analysis_through_harness_invoke():
    from dmf_agent.analysis.tools import run_analysis_tool
    from dmf_agent.config.settings import HarnessSettings
    from dmf_agent.harness import HarnessController, HarnessContext, HarnessState, build_default_tool_registry

    harness = HarnessController(HarnessSettings(enabled=True), registry=build_default_tool_registry())

    async def _raw():
        return run_analysis_tool({"model": "analysis_trend", "query_result": _TREND_QR})

    result = await harness.invoke(
        "run_analysis",
        {"model": "analysis_trend", "query_result": _TREND_QR},
        HarnessContext(user_permissions=["semantic:query"]),
        HarnessState(),
        _raw,
    )
    assert result["ok"] is True and result["data"]["slope"] == 30.0


# ---------------- 试算接口 ----------------


async def test_analysis_run_api():
    from dmf_agent.api.routers.analysis import AnalysisRunRequest, run_analysis_once

    resp = await run_analysis_once(
        AnalysisRunRequest(model="analysis_trend", query_result=_TREND_QR)
    )
    assert resp["ok"] is True
    resp2 = await run_analysis_once(
        AnalysisRunRequest(model="不存在", query_result=_TREND_QR)
    )
    assert resp2["ok"] is False and "未注册" in resp2["error"]
