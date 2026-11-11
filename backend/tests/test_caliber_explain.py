"""FR12 指标计算可解释性：口径说明（公式 / 时间窗 / 过滤 / 同环比 / 版本号）。

覆盖三件事：
1. **口径渲染**：`render_metric_formula` 对 simple / two_stage / ratio / derived 的输出；
2. **口径组装**：`build_caliber` 是否把查询侧要素（时间窗、过滤、同环比、聚合覆盖、
   指标阈值、排序 TopN、行权限）都如实记进结果元信息；
3. **口径落地**：`caliber_lines`／`caliber_markdown`／`caliber_context_text` 的产物，
   以及 `synthesize_answer` 的两条路径（LLM 上下文注入 / 模板答复附注）确实带上了口径。
"""

from __future__ import annotations

import pytest

from dmf_agent.agents.metric_agent.nodes import synthesize_answer as sa
from dmf_agent.semantic.engine import (
    build_caliber,
    caliber_context_text,
    caliber_lines,
    caliber_markdown,
    caliber_version,
    render_metric_formula,
)
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.semantic.manifest import Metric
from dmf_agent.semantic.query import MetricQuery

AS_OF = "2026-08-31"
MODELS = ["retail_deposit_day"]


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer()


def _caliber(layer: SemanticLayer, dsl: dict, *, models=None, row_scopes=None):
    mq = MetricQuery.from_dsl(dsl, layer)
    return (
        build_caliber(
            layer.manifest,
            mq.metrics,
            mq.categorical_group_by,
            list(models or MODELS),
            data_as_of=AS_OF,
            query=mq,
            row_scopes=row_scopes,
        ),
        mq,
    )


def _metric(layer: SemanticLayer, name: str) -> Metric:
    metric = layer.resolve_metric(name)
    assert metric is not None, f"语义层未收录指标：{name}"
    return metric


# --------------------------------------------------------------------------- #
# 1. 口径公式渲染（用真实银行域 manifest，确保断言不是自嗨）
# --------------------------------------------------------------------------- #


def test_formula_simple_grounded_on_real_manifest(layer):
    """simple 指标：`聚合(物理列)`——聚合取自度量定义，不是指标自身。"""
    metric = _metric(layer, "零售存款余额")
    assert render_metric_formula(layer.manifest, metric) == "SUM(deposit_balance)"


def test_formula_two_stage_expands_both_stages(layer):
    """两阶段聚合必须展开成两段，否则用户会以为"日均"是直接 SUM。"""
    metric = _metric(layer, "月日均存款余额")
    formula = render_metric_formula(layer.manifest, metric)
    assert formula == "SUM(逐日 deposit_balance) → AVG(目标粒度)"
    assert "逐日" in formula and "AVG" in formula


def test_formula_ratio_uses_numerator_denominator(layer):
    metric = _metric(layer, "存贷比")
    assert render_metric_formula(layer.manifest, metric) == "零售贷款余额 ÷ 零售存款余额"


def test_formula_derived_prefers_authored_expr(layer):
    """derived 有 expr 时用原文——人工撰写的公式最贴近业务口径，不要自作聪明重排。"""
    metric = _metric(layer, "净利息收入")
    assert render_metric_formula(layer.manifest, metric) == "贷款利息收入 - 存款利息支出"


def test_formula_unknown_measure_falls_back_to_name():
    """度量不在任何模型里（如自定义/脏数据）时回落度量名，不得抛错。"""
    manifest = SemanticManifestStub(metrics=[], models=[])
    metric = Metric(name="野指标", type="simple", type_params={"measure": "not_exist"})
    assert render_metric_formula(manifest, metric) == "not_exist"


def test_formula_derived_without_expr_lists_inputs():
    manifest = SemanticManifestStub(metrics=[], models=[])
    metric = Metric(name="拼的", type="derived", type_params={"metrics": ["A", "B"]})
    assert render_metric_formula(manifest, metric) == "A、B"


# --------------------------------------------------------------------------- #
# 2. 口径组装：查询侧要素必须如实记录
# --------------------------------------------------------------------------- #


def test_caliber_records_time_window_filters_and_period(layer):
    caliber, _ = _caliber(
        layer,
        {
            "metrics": [{"name": "零售存款余额"}],
            "dimensions": ["机构"],
            "filters": [{"field": "机构", "operator": "in", "values": ["杭州分行", "宁波分行"]}],
            "time_range": {"start": "2026-08-01", "end": "2026-08-31", "granularity": "day"},
            "period_comparison": True,
            "period_comparison_types": ["yoy", "mom"],
        },
    )
    assert caliber["time_window"]["text"] == "2026-08-01 ~ 2026-08-31（按日）"
    assert caliber["filters"][0]["text"] == "机构 属于 杭州分行、宁波分行"
    # 同环比必须带**平移语义**（同比=去年同期），不能只给 yoy 三个字母
    assert caliber["period_comparison"]["types"] == ["yoy", "mom"]
    assert caliber["period_comparison"]["labels"] == [
        "同比（去年同期，平移 12 个月）",
        "环比（上一月，平移 1 个月）",
    ]


def test_caliber_records_aggregation_override(layer):
    """「日均」在本项目是聚合覆盖（agg=avg），属口径的一部分，必须能解释。"""
    caliber, _ = _caliber(
        layer,
        {
            "metrics": [{"name": "零售存款余额"}],
            "advanced": {"metric_result": [{"metric_name": "零售存款余额", "agg": "avg"}]},
        },
    )
    assert caliber["aggregation_overrides"] == {"零售存款余额": "avg"}
    assert any("聚合覆盖" in line for line in caliber_lines(caliber))


def test_caliber_records_metric_threshold_and_topn(layer):
    caliber, _ = _caliber(
        layer,
        {
            "metrics": [{"name": "零售存款余额"}],
            "dimensions": ["机构"],
            "metric_filters": [{"metric": "零售存款余额", "operator": ">", "value": 1000000}],
            "order_by": "零售存款余额 desc",
            "limit": 5,
        },
    )
    assert caliber["metric_filters"] == ["零售存款余额 大于 1000000.0"]
    assert caliber["ranking"]["limit"] == 5
    assert caliber["ranking"]["order_by"] == "零售存款余额 desc"


def test_caliber_records_row_permission_scope(layer):
    """行权限导致"数据比全行少"时，用户应能从这里看出来（FR13 可解释性）。

    值集合来自 `set`（迭代顺序不稳定）→ 必须排序后再落盘，否则同一次查询
    每次刷新都可能产出不同的口径内容（也会污染版本号的稳定性预期）。
    """
    caliber, _ = _caliber(
        layer,
        {"metrics": [{"name": "零售存款余额"}], "dimensions": ["机构"]},
        row_scopes={"机构": {"杭州分行", "宁波分行"}},
    )
    assert caliber["row_permission"] == {"机构": sorted(["杭州分行", "宁波分行"])}
    assert any("行权限范围" in line for line in caliber_lines(caliber))


def test_caliber_without_query_still_yields_definitions(layer):
    """兼容只按名称构造的调用方：无 query 时查询侧字段为空，但定义与版本仍产出。"""
    caliber = build_caliber(
        layer.manifest, ["零售存款余额"], ["机构"], MODELS, data_as_of=AS_OF
    )
    assert caliber["metrics"][0]["formula"] == "SUM(deposit_balance)"
    assert caliber["time_window"] == {}
    assert caliber["filters"] == []
    assert caliber["period_comparison"]["enabled"] is False
    assert caliber["version"].startswith("v-")


# --------------------------------------------------------------------------- #
# 3. 口径版本号：稳定 + 对定义变更敏感
# --------------------------------------------------------------------------- #


def test_caliber_version_ignores_metric_order():
    """同一次口径下，指标书写顺序不同不应产生不同版本号（否则前端会频繁"变版本"）。"""
    a = build_caliber(
        SemanticManifestStub(metrics=[_simple("A"), _simple("B")]),
        ["A", "B"],
        [],
        ["m"],
    )
    b = build_caliber(
        SemanticManifestStub(metrics=[_simple("A"), _simple("B")]),
        ["B", "A"],
        [],
        ["m"],
    )
    assert a["version"] == b["version"]


def test_caliber_version_changes_when_definition_changes():
    """口径文案/公式一改，版本号必须变——否则追溯不到"当时按哪版口径算的"。"""
    base = _simple("GMV")
    tweaked = _simple("GMV")
    tweaked.description = base.description + "（口径修订）"
    v1 = build_caliber(SemanticManifestStub(metrics=[base]), ["GMV"], [], ["m"])["version"]
    v2 = build_caliber(SemanticManifestStub(metrics=[tweaked]), ["GMV"], [], ["m"])["version"]
    assert v1 != v2


def test_caliber_version_is_hash_of_definitions_only():
    """版本号只对定义取摘要：与时间窗/过滤等查询参数无关。"""
    core = {"metrics": [{"name": "A", "formula": "SUM(x)"}], "dimensions": [], "models": ["m"]}
    assert caliber_version(core) == caliber_version(dict(core))
    assert caliber_version(core).startswith("v-")
    assert len(caliber_version(core)) == 12  # "v-" + 10 位十六进制


# --------------------------------------------------------------------------- #
# 4. 口径落地：渲染产物 + 答复路径
# --------------------------------------------------------------------------- #


def test_caliber_lines_cover_fr12_facets(layer):
    caliber, _ = _caliber(
        layer,
        {
            "metrics": [{"name": "存贷比"}],
            "dimensions": ["机构"],
            "filters": [{"field": "机构", "operator": "=", "values": ["杭州分行"]}],
            "time_range": {"start": "2026-08-01", "end": "2026-08-31", "granularity": "day"},
            "period_comparison": True,
            "period_comparison_type": "mom",
        },
    )
    lines = "\n".join(caliber_lines(caliber))
    # FR12 要求的四要素：公式 / 同环比定义 / 过滤条件 / 时间窗
    assert "指标「存贷比」：零售贷款余额 ÷ 零售存款余额" in lines
    assert "同环比：环比（上一月，平移 1 个月）" in lines
    assert "过滤条件：机构 等于 杭州分行" in lines
    assert "时间窗：2026-08-01 ~ 2026-08-31（按日）" in lines
    # 数据来源标注 + 版本号
    assert "数据源：retail_deposit_day" in lines
    assert f"口径版本：{caliber['version']}" in lines
    assert f"数据截至：{AS_OF}" in lines


def test_caliber_markdown_has_header(layer):
    caliber, _ = _caliber(layer, {"metrics": [{"name": "零售存款余额"}]})
    md = caliber_markdown(caliber)
    assert md.startswith("\n\n**本次结果口径说明**")
    assert "SUM(deposit_balance)" in md


def test_caliber_markdown_empty_when_no_caliber():
    assert caliber_markdown({}) == ""
    assert caliber_markdown(None) == ""
    assert caliber_context_text({}) == ""


def test_caliber_context_text_is_bulleted_facts(layer):
    caliber, _ = _caliber(layer, {"metrics": [{"name": "零售存款余额"}]})
    text = caliber_context_text(caliber)
    assert text.startswith("- 指标「零售存款余额」")
    assert "\n- " in text


def test_synthesize_context_injects_caliber_as_fact(layer):
    """LLM 路径：口径必须以「事实」身份进上下文，并带禁止改写的约束。"""
    caliber, _ = _caliber(layer, {"metrics": [{"name": "零售存款余额"}]})
    state = {
        "intent": "metric_query",
        "query_result": {"caliber": caliber},
        "query_elements": {"metrics": ["零售存款余额"]},
    }
    context = sa._build_context(state)
    assert "本次结果口径" in context
    assert "不得改写公式或同环比定义" in context
    assert "SUM(deposit_balance)" in context


def test_template_answer_appends_deterministic_caliber(layer):
    """模板路径（无 LLM）：答复正文直接附确定性口径块，不能"静默没有口径"。"""
    caliber, _ = _caliber(
        layer,
        {
            "metrics": [{"name": "零售存款余额"}],
            "dimensions": ["机构"],
            "time_range": {"start": "2026-08-01", "end": "2026-08-31", "granularity": "day"},
        },
    )
    answer = sa._template_answer(
        {
            "intent": "metric_query",
            "analysis": {"stats": {"零售存款余额": {"total": 1, "avg": 1, "max": 1, "min": 1}}},
            "query_result": {
                "caliber": caliber,
                "columns": ["机构", "零售存款余额"],
                "rows": [["杭州分行", 1]],
                "row_count": 1,
                "elapsed_ms": 12,
            },
        }
    )
    assert "本次结果口径说明" in answer
    assert "SUM(deposit_balance)" in answer
    assert "2026-08-01 ~ 2026-08-31（按日）" in answer
    assert f"口径版本：{caliber['version']}" in answer


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #


class SemanticManifestStub:
    """最小语义清单（只喂 metrics / models），用于版本号等无需真实清单的断言。"""

    def __init__(self, metrics=(), models=()):
        self.metrics = list(metrics)
        self.models = list(models)


def _simple(name: str) -> Metric:
    return Metric(
        name=name,
        type="simple",
        type_params={"measure": f"{name}_measure"},
        unit="元",
        description=f"{name} 口径说明",
    )
