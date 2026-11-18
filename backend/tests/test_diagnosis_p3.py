"""归因优化 P3 测试（2026-11-17）：因子归因（逐因子替换法）+ 指标树归因 + manifest 扩展。

覆盖：
- factors.parse_factor_expr / factor_contribution：表达式解析、替换法数值（手算对拍）、
  交互项（顺序尾差）恒等式、除法/未声明因子拒绝
- tree.tree_attribution：单层分解、share 在可用子集内归一、残差、缺失子项、树缺失返回 None
- manifest：factors 公式校验（未声明因子/未定义引用报错）、metrics_tree 双写法解析与引用校验
- 节点接线：默认清单（无 factors/树）自动跳过；带 factors 的层触发因子归因（含取数 DSL 与
  失败降级）；开关关闭跳过
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dmf_agent.diagnosis.factors import factor_contribution, parse_factor_expr, summarize_factors
from dmf_agent.diagnosis.tree import summarize_tree, tree_attribution

# ---------------- parse_factor_expr ----------------


def test_parse_factor_expr_ok():
    ordered = parse_factor_expr("uv * cvr * arpu", {"uv": "访客数", "cvr": "转化率", "arpu": "客单价"})
    assert ordered == ["uv", "cvr", "arpu"]


def test_parse_factor_expr_chinese_and_parens():
    ordered = parse_factor_expr("(uv) * cvr", {"uv": "访客数", "cvr": "转化率"})
    assert ordered == ["uv", "cvr"]


def test_parse_factor_expr_rejects_undeclared():
    assert parse_factor_expr("uv * pv", {"uv": "访客数"}) is None


def test_parse_factor_expr_rejects_non_multiply():
    assert parse_factor_expr("uv / cvr", {"uv": "访客数", "cvr": "转化率"}) is None
    assert parse_factor_expr("uv + cvr", {"uv": "访客数", "cvr": "转化率"}) is None


def test_parse_factor_expr_rejects_empty():
    assert parse_factor_expr("", {"uv": "访客数"}) is None


# ---------------- factor_contribution（手算对拍） ----------------


def test_factor_contribution_two_factors():
    # baseline: uv=100, cvr=0.5 → 总量 50；current: uv=120, cvr=0.4 → 总量 48
    result = factor_contribution(
        {"uv": 100.0, "cvr": 0.5}, {"uv": 120.0, "cvr": 0.4}, ["uv", "cvr"]
    )
    # 逐因子替换（基线=上期）：先换 uv：0.5*120 - 0.5*100 = 10；再换 cvr：0.4*120 - 0.5*120 = -12
    by = {f["factor"]: f for f in result["factors"]}
    assert by["uv"]["contribution"] == 10
    assert by["cvr"]["contribution"] == -12
    assert result["total_change"] == -2
    # 恒等式：Σ贡献 + interaction = 总变化
    assert round(sum(f["contribution"] for f in result["factors"]) + result["interaction"], 4) == result["total_change"]
    assert result["baseline_total"] == 50
    assert result["current_total"] == 48


def test_factor_contribution_interaction_captures_residual():
    """交互项 = 顺序尾差（两因子同时变化时替换法有残差，显式输出不摊派）。"""
    result = factor_contribution(
        {"uv": 100.0, "cvr": 0.5}, {"uv": 200.0, "cvr": 1.0}, ["uv", "cvr"]
    )
    # base 50 → curr 200，总变化 150；uv 贡献 50、cvr 贡献 100、交互 0（此例恰无残差）
    by = {f["factor"]: f for f in result["factors"]}
    assert by["uv"]["contribution"] == 50
    assert by["cvr"]["contribution"] == 100
    assert result["interaction"] == 0
    assert result["total_change"] == 150


def test_factor_contribution_three_factors_identity():
    """三因子：Σ贡献 + interaction 恒等于总变化（任意变化组合）。"""
    result = factor_contribution(
        {"a": 10.0, "b": 3.0, "c": 2.0}, {"a": 12.0, "b": 1.0, "c": 5.0}, ["a", "b", "c"]
    )
    lhs = sum(f["contribution"] for f in result["factors"]) + result["interaction"]
    assert round(lhs, 4) == result["total_change"]
    assert result["baseline_total"] == 60
    assert result["current_total"] == 60


def test_factor_contribution_zero_baseline():
    """基线为 0（新指标）→ rate 为 None，不编造比率。"""
    result = factor_contribution({"uv": 0.0, "cvr": 0.0}, {"uv": 100.0, "cvr": 0.5}, ["uv", "cvr"])
    by = {f["factor"]: f for f in result["factors"]}
    assert by["uv"]["rate"] is None
    assert result["total_change"] == 50


def test_summarize_factors_text():
    result = factor_contribution({"uv": 100.0, "cvr": 0.5}, {"uv": 120.0, "cvr": 0.4}, ["uv", "cvr"])
    text = summarize_factors("GMV", result)
    assert "GMV" in text and "uv" in text and "cvr" in text


# ---------------- tree_attribution ----------------


TREE = [
    {"parent": "GMV", "child": "订单数", "weight": 1.0},
    {"parent": "GMV", "child": "客单价", "weight": 1.0},
]


def test_tree_attribution_basic():
    period = {
        "GMV": {"change_value": 100, "metrics_value": 1100, "compare_metrics_value": 1000},
        "订单数": {"change_value": 60, "metrics_value": 660, "compare_metrics_value": 600},
        "客单价": {"change_value": 30, "metrics_value": 330, "compare_metrics_value": 300},
    }
    result = tree_attribution(TREE, "GMV", period)
    assert result is not None
    assert result["root_change"] == 100
    by = {c["child"]: c for c in result["children"]}
    assert by["订单数"]["change"] == 60
    assert by["客单价"]["change"] == 30
    # share 在可用子集内归一：60/(60+30)=0.6667
    assert by["订单数"]["share"] == 0.6667
    # 残差 = 100 - 90 = 10（父口径与子口径差，如实输出）
    assert result["residual"] == 10


def test_tree_attribution_missing_child_marked_unavailable():
    period = {
        "GMV": {"change_value": 100, "metrics_value": 1100, "compare_metrics_value": 1000},
        "订单数": {"change_value": 60, "metrics_value": 0, "compare_metrics_value": 0},
        # 客单价缺失
    }
    result = tree_attribution(TREE, "GMV", period)
    by = {c["child"]: c for c in result["children"]}
    assert by["客单价"]["available"] is False
    assert by["订单数"]["share"] == 1.0  # share 在可用子集内归一
    assert "客单价" in summarize_tree(result)


def test_tree_attribution_no_root_change_none():
    assert tree_attribution(TREE, "GMV", {}) is None


def test_tree_attribution_no_children_edges_none():
    period = {"孤立指标": {"change_value": 5, "metrics_value": 5, "compare_metrics_value": 0}}
    assert tree_attribution(TREE, "孤立指标", period) is None


# ---------------- manifest 扩展 ----------------


def _write_manifest(tmp_path, raw):
    p = tmp_path / "manifest.yml"
    p.write_text(raw, encoding="utf-8")
    from dmf_agent.semantic.manifest import SemanticManifest

    return SemanticManifest(p)


def test_manifest_factors_validation_undeclared_factor(tmp_path):
    import pytest

    from dmf_agent.semantic.manifest import ManifestError

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
metrics:
  - name: A
    type: simple
    type_params: {measure: gmv}
  - name: GMV
    type: simple
    type_params: {measure: gmv}
    factors:
      expr: "uv * cvr"
      factors: {uv: A, cvr: 未定义指标}
"""
    with pytest.raises(ManifestError, match="未定义"):
        _write_manifest(tmp_path, raw)


def test_manifest_factors_validation_expr_undeclared_key(tmp_path):
    import pytest

    from dmf_agent.semantic.manifest import ManifestError

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
metrics:
  - name: A
    type: simple
    type_params: {measure: gmv}
  - name: GMV
    type: simple
    type_params: {measure: gmv}
    factors:
      expr: "uv * pv"
      factors: {uv: A}
"""
    with pytest.raises(ManifestError, match="未声明的因子"):
        _write_manifest(tmp_path, raw)


def test_manifest_metrics_tree_both_syntaxes(tmp_path):
    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}}
  - {name: 订单数, type: simple, type_params: {measure: gmv}}
  - {name: 客单价, type: simple, type_params: {measure: gmv}}
  - {name: 其它, type: simple, type_params: {measure: gmv}}
metrics_tree:
  - node: GMV
    children: [订单数, 客单价]
  - {parent: 订单数, child: 其它}
"""
    manifest = _write_manifest(tmp_path, raw)
    assert len(manifest.metrics_tree) == 3
    parents = {(e["parent"], e["child"]) for e in manifest.metrics_tree}
    assert ("GMV", "订单数") in parents and ("订单数", "其它") in parents


def test_manifest_metrics_tree_bad_reference(tmp_path):
    import pytest

    from dmf_agent.semantic.manifest import ManifestError

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}}
metrics_tree:
  - node: GMV
    children: [不存在的子指标]
"""
    with pytest.raises(ManifestError, match="不存在的指标"):
        _write_manifest(tmp_path, raw)


def test_manifest_metrics_tree_malformed_items_skipped(tmp_path):
    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}}
metrics_tree:
  - "垃圾字符串"
  - {foo: bar}
  - {node: GMV, children: "不是列表"}
"""
    manifest = _write_manifest(tmp_path, raw)
    assert manifest.metrics_tree == []


# ---------------- 节点接线 ----------------


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


def _significant_qr():
    return {
        "columns": ["统计日期", "渠道", "GMV"],
        "rows": [
            ["2026-08-01", "APP", 100],
            ["2026-08-02", "APP", 100],
            ["2026-08-03", "APP", 200],
            ["2026-08-04", "APP", 200],
        ],
    }


def test_diagnose_default_manifest_skips_p3():
    """默认清单无 factors / metrics_tree -> P3 全部静默跳过（零取数、无 warnings）。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    assert "factors" not in diag and "tree" not in diag
    assert not [w for w in diag.get("warnings") or [] if "因子" in w or "树" in w]


def test_diagnose_factors_with_custom_layer(tmp_path):
    """带 factors 公式的语义层 -> 因子归因触发，逐因子补取数并产出贡献。"""
    from dmf_agent.semantic.layer import SemanticLayer
    from fakes import FakeMetricEngine, make_deps

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
      - {name: 渠道, type: categorical, expr: ch}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
      - {name: uv, agg: sum, expr: uv}
      - {name: cvr, agg: sum, expr: cvr}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}, factors: {expr: "uv * cvr", factors: {uv: 访客数, cvr: 转化率}}}
  - {name: 访客数, type: simple, type_params: {measure: uv}}
  - {name: 转化率, type: simple, type_params: {measure: cvr}}
"""
    mfile = tmp_path / "manifest.yml"
    mfile.write_text(raw, encoding="utf-8")
    layer = SemanticLayer(mfile)
    engine = FakeMetricEngine(layer)
    deps = make_deps(layer=layer, engine=engine)
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    factors = diag.get("factors")
    assert factors and factors["metric"] == "GMV"
    by = {f["factor"]: f for f in factors["factors"]}
    assert set(by) == {"uv", "cvr"}
    # 恒等式闭合
    lhs = sum(f["contribution"] for f in factors["factors"]) + factors["interaction"]
    assert round(lhs, 4) == factors["total_change"]
    # 每个因子各一次取数（GMV 已有数据，未触发补维度；因子 2 次）
    assert len(engine.queries) == 2


def test_diagnose_factors_fetch_failure_skips_with_warning(tmp_path):
    """因子取数失败 -> 整体跳过因子归因 + 如实告警，主结论不受影响。"""
    from dmf_agent.semantic.layer import SemanticLayer
    from fakes import FakeMetricEngine, make_deps

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
      - {name: 渠道, type: categorical, expr: ch}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
      - {name: uv, agg: sum, expr: uv}
      - {name: cvr, agg: sum, expr: cvr}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}, factors: {expr: "uv * cvr", factors: {uv: 访客数, cvr: 转化率}}}
  - {name: 访客数, type: simple, type_params: {measure: uv}}
  - {name: 转化率, type: simple, type_params: {measure: cvr}}
"""
    mfile = tmp_path / "manifest.yml"
    mfile.write_text(raw, encoding="utf-8")
    layer = SemanticLayer(mfile)
    deps = make_deps(layer=layer, engine=FakeMetricEngine(layer, fail_with="因子查询失败"))
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    assert "factors" not in diag
    assert diag["warnings"] and "因子" in " ".join(diag["warnings"])


def test_diagnose_factors_disabled_by_switch(tmp_path):
    from dmf_agent.semantic.layer import SemanticLayer
    from fakes import FakeMetricEngine, make_deps

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
      - {name: 渠道, type: categorical, expr: ch}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
      - {name: uv, agg: sum, expr: uv}
      - {name: cvr, agg: sum, expr: cvr}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}, factors: {expr: "uv * cvr", factors: {uv: 访客数, cvr: 转化率}}}
  - {name: 访客数, type: simple, type_params: {measure: uv}}
  - {name: 转化率, type: simple, type_params: {measure: cvr}}
"""
    mfile = tmp_path / "manifest.yml"
    mfile.write_text(raw, encoding="utf-8")
    layer = SemanticLayer(mfile)
    deps = make_deps(layer=layer, engine=FakeMetricEngine(layer))
    deps.settings.diagnosis.factors_enabled = False
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _significant_qr())
    result = asyncio.run(node(state))
    assert "factors" not in result["diagnosis"]


def test_diagnose_tree_with_custom_layer(tmp_path):
    """带 metrics_tree 的语义层 -> 树归因触发，子指标逐个补取数并产出分解。"""
    from dmf_agent.semantic.layer import SemanticLayer
    from fakes import FakeMetricEngine, make_deps

    raw = """
semantic_models:
  - name: m
    table: t
    agg_time_dimension: 统计日期
    dimensions:
      - {name: 统计日期, type: time, expr: d}
      - {name: 渠道, type: categorical, expr: ch}
    measures:
      - {name: gmv, agg: sum, expr: gmv}
      - {name: ord, agg: sum, expr: ord}
metrics:
  - {name: GMV, type: simple, type_params: {measure: gmv}}
  - {name: 订单数, type: simple, type_params: {measure: ord}}
metrics_tree:
  - node: GMV
    children: [订单数]
"""
    mfile = tmp_path / "manifest.yml"
    mfile.write_text(raw, encoding="utf-8")
    layer = SemanticLayer(mfile)
    engine = FakeMetricEngine(layer)
    deps = make_deps(layer=layer, engine=engine)
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    tree = diag.get("tree")
    assert tree and tree["root"] == "GMV"
    children = {c["child"]: c for c in tree["children"]}
    assert "订单数" in children and children["订单数"]["available"] is True
    # 子指标取数 1 次
    assert len(engine.queries) == 1


# ---------------- 多指标联合归因（2026-11-17 遗留清理） ----------------


def _multi_qr():
    """3 指标 × 2 渠道 × 4 时间点：GMV 跌、订单数涨（背离）、付费用户数持平。"""
    return {
        "columns": ["统计日期", "渠道", "GMV", "订单数", "付费用户数"],
        "rows": [
            ["2026-08-01", "APP", 100, 10, 5],
            ["2026-08-01", "H5", 50, 8, 4],
            ["2026-08-02", "APP", 100, 10, 5],
            ["2026-08-02", "H5", 50, 8, 4],
            ["2026-08-03", "APP", 60, 20, 5],
            ["2026-08-03", "H5", 60, 16, 4],
            ["2026-08-04", "APP", 60, 20, 5],
            ["2026-08-04", "H5", 60, 16, 4],
        ],
    }


def test_diagnose_multi_metric_all_attributed():
    """多指标问句：主指标顶层字段不变，其余指标各自独立归因进 per_metric。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.drilldown_enabled = False
    deps.settings.diagnosis.cross_enabled = False
    deps.settings.diagnosis.factors_enabled = False
    deps.settings.diagnosis.tree_enabled = False
    node, state = _node_and_state(deps, ["渠道"], _multi_qr())
    state["dsl"]["metrics"] = [
        {"name": "GMV"},
        {"name": "订单数"},
        {"name": "付费用户数"},
    ]
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    assert diag["metric"] == "GMV"  # 主指标 = metrics[0]，顶层字段向后兼容
    per = {e["metric"]: e for e in diag.get("per_metric") or []}
    assert set(per) == {"订单数", "付费用户数"}
    assert per["订单数"]["ok"] is True
    assert per["付费用户数"]["ok"] is True
    # 各自独立期间归因（GMV -60、订单数 +36、付费用户数 0）
    assert diag["change"]["change_value"] == -60
    assert per["订单数"]["change"]["change_value"] == 36
    assert per["付费用户数"]["change"]["change_value"] == 0
    # 零额外取数（纯计算）
    assert len(deps.metric_engine.queries) == 0


def test_diagnose_metric_relations_directions():
    """方向关系确定性判定：同向/背离/持平；不做因果推断。"""
    from fakes import make_deps

    deps = make_deps()
    for key in ("drilldown_enabled", "cross_enabled", "factors_enabled", "tree_enabled"):
        setattr(deps.settings.diagnosis, key, False)
    node, state = _node_and_state(deps, ["渠道"], _multi_qr())
    state["dsl"]["metrics"] = [{"name": "GMV"}, {"name": "订单数"}, {"name": "付费用户数"}]
    result = asyncio.run(node(state))
    rel = {r["metric"]: r for r in result["diagnosis"].get("metric_relations") or []}
    assert rel["订单数"]["relation"] == "背离"  # GMV 跌、订单数涨
    assert rel["付费用户数"]["relation"] == "持平"
    assert all(r["vs"] == "GMV" for r in rel.values())


def test_diagnose_multi_metric_missing_column_reported():
    """DSL 声明了但取数结果没有的指标 -> per_metric 如实报缺，主指标照常归因。"""
    from fakes import make_deps

    deps = make_deps()
    for key in ("drilldown_enabled", "cross_enabled", "factors_enabled", "tree_enabled"):
        setattr(deps.settings.diagnosis, key, False)
    node, state = _node_and_state(deps, ["渠道"], _multi_qr())
    state["dsl"]["metrics"] = [{"name": "GMV"}, {"name": "不存在的指标"}]
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    per = diag.get("per_metric") or []
    assert len(per) == 1 and per[0]["ok"] is False and "缺少该指标列" in per[0]["message"]


def test_diagnose_secondary_insufficient_points():
    """次要指标时间点不足 -> 该指标 ok=false，主指标与其它次要指标不受影响。"""
    from fakes import make_deps

    deps = make_deps()
    for key in ("drilldown_enabled", "cross_enabled", "factors_enabled", "tree_enabled"):
        setattr(deps.settings.diagnosis, key, False)
    qr = {
        "columns": ["统计日期", "渠道", "GMV", "订单数"],
        "rows": [
            ["2026-08-01", "APP", 100, 10],
            ["2026-08-02", "APP", 100, None],
            ["2026-08-03", "APP", 200, None],
            ["2026-08-04", "APP", 200, None],
        ],
    }
    node, state = _node_and_state(deps, ["渠道"], qr)
    state["dsl"]["metrics"] = [{"name": "GMV"}, {"name": "订单数"}]
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True and diag["metric"] == "GMV"
    per = diag.get("per_metric") or []
    assert len(per) == 1 and per[0]["ok"] is False and "不足两个" in per[0]["message"]


def test_diagnose_metric_relations_helper_direct():
    """_metric_relations 纯函数：主指标零变化时次要指标涨跌均记「背离」。"""
    from dmf_agent.agents.metric_agent.nodes.diagnose import _metric_relations

    secondaries = [
        {
            "ok": True,
            "metric": "B",
            "change": {"change_value": 10, "change_rate": 0.1},
        },
        {
            "ok": True,
            "metric": "C",
            "change": {"change_value": -10, "change_rate": -0.1},
        },
    ]
    rel = _metric_relations("A", {"change_value": 0}, secondaries)
    by = {r["metric"]: r for r in rel}
    assert by["B"]["relation"] == "背离"  # 主指标 0（非正），B 涨 -> 方向不一致
    assert by["C"]["relation"] == "同向"  # C 跌与主指标 0 同记非正方向


def test_decomposer_keeps_diagnosis_multi_metric_intact():
    """归因意图的多指标问句不被 FR10 拆分（联合归因依赖同一次取数的全部指标列）。"""
    from dmf_agent.agents.metric_agent.decomposer import decompose_query
    from fakes import make_deps

    deps = make_deps()
    tasks = decompose_query("近7天GMV和订单数为什么下降了", deps=deps)
    assert len(tasks) == 1
    assert tasks[0].intent == "diagnosis"


def test_decomposer_still_splits_plain_multi_metric_query():
    """非归因的普通并列查询仍按 FR10 拆分（行为不变）。"""
    from dmf_agent.agents.metric_agent.decomposer import decompose_query
    from fakes import make_deps

    deps = make_deps()
    tasks = decompose_query("查一下GMV和订单数", deps=deps)
    assert len(tasks) == 2
