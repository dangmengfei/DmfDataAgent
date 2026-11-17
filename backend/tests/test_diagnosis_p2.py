"""归因优化 P2 测试（2026-11-16）：两维交叉归因 + 递归下钻 + LLM 规划 + 建议召回。

覆盖：
- core.cross_attribution：交叉矩阵、children 可加和还原 cell 总变化、组合截断保整 cell
- 节点级：交叉归因触发条件、规则兜底下钻（过滤到父维值 × 新维度）、下钻取数 DSL 剥离
  排序/TopN、下钻失败降级、异动非 significant 时不触发下钻、下钻关闭开关
- LLM 规划：合法 drill 被采纳 / 非法动作回落规则 / stop 终止
- 行动建议召回：命中注入 advice_docs、无权限/无命中降级保留模板建议
- 确定性：并列变化量的 TOP 维值顺序稳定（set 迭代顺序曾导致漂移）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dmf_agent.diagnosis.core import cross_attribution, rows_to_dicts

# 2 个时间点 × 2 渠道 × 2 地区：
# 上期：APP(上海 100, 北京 50) = 150，H5(上海 30, 北京 20) = 50，总计 200
# 本期：APP(上海 60, 北京 40) = 100（-50），H5(上海 30, 北京 20) = 50（0），总计 150（-50）
QR_CROSS = {
    "columns": ["统计日期", "渠道", "地区", "GMV"],
    "rows": [
        ["2026-08-01", "APP", "上海", 100],
        ["2026-08-01", "APP", "北京", 50],
        ["2026-08-01", "H5", "上海", 30],
        ["2026-08-01", "H5", "北京", 20],
        ["2026-08-02", "APP", "上海", 60],
        ["2026-08-02", "APP", "北京", 40],
        ["2026-08-02", "H5", "上海", 30],
        ["2026-08-02", "H5", "北京", 20],
    ],
}


# ---------------- core.cross_attribution ----------------


def test_cross_attribution_cells_and_children():
    rows = rows_to_dicts(QR_CROSS)
    cross = cross_attribution(rows, "统计日期", "渠道", "地区", "GMV")
    assert cross is not None
    assert cross["dim_a"] == "渠道" and cross["dim_b"] == "地区"
    assert cross["truncated"] is False
    cells = {c["dim_a_value"]: c for c in cross["cells"]}
    assert set(cells) == {"APP", "H5"}
    app = cells["APP"]
    # APP：上期 150 → 本期 100，变化 -50
    assert app["prev"] == 150
    assert app["curr"] == 100
    assert app["change_value"] == -50
    # children 可加和还原 cell 总变化（不变量）
    children = {c["dim_value"]: c for c in app["children"]}
    assert children["上海"]["change_value"] == -40  # 100 → 60
    assert children["北京"]["change_value"] == -10  # 50 → 40
    assert sum(c["change_value"] for c in app["children"]) == app["change_value"]
    # H5 无变化
    assert cells["H5"]["change_value"] == 0


def test_cross_attribution_cells_sorted_by_abs_change():
    rows = rows_to_dicts(QR_CROSS)
    cross = cross_attribution(rows, "统计日期", "渠道", "地区", "GMV")
    values = [abs(c["change_value"]) for c in cross["cells"]]
    assert values == sorted(values, reverse=True)


def test_cross_attribution_truncates_whole_cells():
    """组合基数超限时按整 cell 截断（保持 children 可加和还原 cell 总变化）。"""
    rows = []
    for i in range(30):
        for t, v in (("2026-08-01", 100), ("2026-08-02", 10)):
            rows.append({"统计日期": t, "渠道": f"C{i}", "地区": "上海", "GMV": v})
    cross = cross_attribution(rows, "统计日期", "渠道", "地区", "GMV", max_combos=5)
    assert cross["truncated"] is True
    assert cross["total_combos"] == 30
    assert len(cross["cells"]) == 5
    for cell in cross["cells"]:
        assert sum(c["change_value"] for c in cell["children"]) == cell["change_value"]


def test_cross_attribution_insufficient_points_none():
    rows = [{"统计日期": "2026-08-01", "渠道": "APP", "地区": "上海", "GMV": 100}]
    assert cross_attribution(rows, "统计日期", "渠道", "地区", "GMV") is None


def test_cross_attribution_structure_change_presence():
    """新增/消失的组合标 presence（不与涨跌混排）。"""
    rows = [
        {"统计日期": "2026-08-01", "渠道": "APP", "地区": "上海", "GMV": 100},
        {"统计日期": "2026-08-02", "渠道": "APP", "地区": "北京", "GMV": 100},
    ]
    cross = cross_attribution(rows, "统计日期", "渠道", "地区", "GMV")
    children = {c["dim_value"]: c for c in cross["cells"][0]["children"]}
    assert children["上海"]["presence"] == "removed"
    assert children["北京"]["presence"] == "added"


# ---------------- 节点级：交叉归因触发条件 ----------------


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
    """显著变化的期间数据：上期 2 天恒定 100，本期 2 天恒定 200（变化 100%）。"""
    return {
        "columns": ["统计日期", "渠道", "地区", "GMV"],
        "rows": [
            ["2026-08-01", "APP", "上海", 100],
            ["2026-08-01", "APP", "北京", 50],
            ["2026-08-02", "APP", "上海", 100],
            ["2026-08-02", "APP", "北京", 50],
            ["2026-08-03", "APP", "上海", 200],
            ["2026-08-03", "APP", "北京", 100],
            ["2026-08-04", "APP", "上海", 200],
            ["2026-08-04", "APP", "北京", 100],
        ],
    }


def test_diagnose_computes_cross_when_two_dims():
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.drilldown_enabled = False  # 隔离下钻，只验交叉
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True
    cross = diag.get("cross")
    assert cross and cross["dim_a"] == "渠道" and cross["dim_b"] == "地区"
    assert cross["cells"]


def test_diagnose_no_cross_with_single_dim():
    """只有一个业务维度 -> 无交叉（材料不足，不硬凑）。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.drilldown_enabled = False
    qr = {
        "columns": ["统计日期", "渠道", "GMV"],
        "rows": [
            ["2026-08-01", "APP", 100],
            ["2026-08-02", "APP", 100],
            ["2026-08-03", "APP", 200],
            ["2026-08-04", "APP", 200],
        ],
    }
    node, state = _node_and_state(deps, ["渠道"], qr)
    result = asyncio.run(node(state))
    assert "cross" not in result["diagnosis"]


def test_diagnose_cross_disabled_by_switch():
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.cross_enabled = False
    deps.settings.diagnosis.drilldown_enabled = False
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    assert "cross" not in result["diagnosis"]


# ---------------- 节点级：规则兜底下钻 ----------------


def test_diagnose_rule_drilldown_filters_parent_value():
    """规则兜底下钻：过滤到主维度 TOP 维值 × 新维度，且剥离排序/TopN。"""
    from fakes import make_deps

    deps = make_deps()
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    drilldown = diag.get("drilldown")
    assert drilldown and len(drilldown) == 1
    node0 = drilldown[0]
    assert node0["parent_dim"] == "渠道"
    assert node0["dim"] == "品类"  # 候选维度里的新维度（声明顺序）
    assert node0["children"]  # 下钻层有结果
    # 下钻取数：过滤到父维值 + 时间维度 + 新维度；剥离 order_by/limit
    drill_query = deps.metric_engine.queries[-1]
    assert drill_query["dimensions"] == ["统计日期", "品类"]
    assert drill_query["filters"] == [
        {"dimension": "渠道", "dimension_value": ["APP"], "filter_operator": "in"}
    ]
    assert "order_by" not in drill_query and "limit" not in drill_query
    assert "advanced" not in drill_query


def test_diagnose_drilldown_skipped_for_mild_anomaly():
    """轻度波动不触发下钻（不值得额外取数深挖）。"""
    from fakes import make_deps

    deps = make_deps()
    qr = {
        "columns": ["统计日期", "渠道", "地区", "GMV"],
        "rows": [
            ["2026-08-01", "APP", "上海", 100],
            ["2026-08-02", "APP", "上海", 100],
            ["2026-08-03", "APP", "上海", 101],
            ["2026-08-04", "APP", "上海", 101],
        ],
    }
    node, state = _node_and_state(deps, ["渠道", "地区"], qr)
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["anomaly"]["status"] in ("mild", "noise")
    assert not diag.get("drilldown")


def test_diagnose_drilldown_disabled_by_switch():
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.drilldown_enabled = False
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    assert not result["diagnosis"].get("drilldown")


def test_diagnose_drilldown_fetch_failure_degrades_with_warning():
    """下钻取数失败 -> 保留已完成结论 + 如实告警，不中断。"""
    from fakes import FakeMetricEngine, make_deps

    deps = make_deps()
    engine = FakeMetricEngine(deps.semantic_layer)
    deps.metric_engine = engine
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())

    # 让下钻取数失败（query_result 是预置的，引擎只在补取数/下钻时被调用，
    # 故此处第 1 次调用即失败）
    calls = {"n": 0}

    async def _failing_query(source, row_scopes=None):
        calls["n"] += 1
        raise RuntimeError("下钻取数失败")

    engine.query = _failing_query
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert diag["ok"] is True  # 主结论仍在
    assert not diag.get("drilldown")
    assert diag["warnings"] and "下钻取数失败" in " ".join(diag["warnings"])


def test_diagnose_drilldown_respects_max_depth():
    """下钻层数不超过 max_drill_depth（每层一次取数，预算受控）。"""
    from fakes import make_deps

    deps = make_deps()
    deps.settings.diagnosis.max_drill_depth = 1
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    assert len(result["diagnosis"].get("drilldown") or []) <= 1


# ---------------- LLM 规划 ----------------


class _FakeLLM:
    """最小 LLM 替身：返回预置的结构化规划结果。"""

    def __init__(self, step):
        self.step = step

    async def ainvoke(self, messages, **kwargs):
        return self

    async def agenerate_patches(self, *args, **kwargs):  # pragma: no cover
        return None

    def __getattr__(self, name):
        # astructured_invoke 会尝试多种通道（with_structured_output 等）；
        # 这里统一走「提示词兜底」通道需要 invoke 返回文本，故返回 JSON 文本。
        if name in ("with_structured_output", "bind_tools"):
            raise AttributeError(name)
        raise AttributeError(name)


def _patch_structured(monkeypatch, step_dict):
    """把 astructured_invoke 打桩为返回指定结构（避免依赖真实 LLM 通道）。"""
    from dmf_agent.schemas.agent import DiagnosisPlanStep

    async def _fake(llm, schema, messages, **kwargs):
        return DiagnosisPlanStep(**step_dict)

    monkeypatch.setattr("dmf_agent.llm.structured.astructured_invoke", _fake)


def test_diagnose_llm_plan_drill_accepted(monkeypatch):
    """LLM 规划 drill（合法：维度在候选内、维值在已知集合）-> 被采纳。"""
    from fakes import make_deps

    _patch_structured(
        monkeypatch,
        {
            "action": "drill",
            "parent_dim": "渠道",
            "value": "APP",
            "dim": "品类",
            "reason": "APP 是主要贡献者，按品类定位",
        },
    )
    deps = make_deps(get_chat_model=lambda: _FakeLLM(None))
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    drilldown = result["diagnosis"].get("drilldown")
    assert drilldown and len(drilldown) == 1
    assert drilldown[0]["dim"] == "品类"
    assert drilldown[0]["reason"] == "APP 是主要贡献者，按品类定位"


def test_diagnose_llm_plan_stop_terminates(monkeypatch):
    """LLM 规划 stop -> 不再下钻（无额外取数）。"""
    from fakes import make_deps

    _patch_structured(
        monkeypatch,
        {"action": "stop", "parent_dim": "", "value": "", "dim": "", "reason": "已足够"},
    )
    deps = make_deps(get_chat_model=lambda: _FakeLLM(None))
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    assert not result["diagnosis"].get("drilldown")
    assert len(deps.metric_engine.queries) == 0  # 未发生补取数


def test_diagnose_llm_plan_invalid_falls_back_to_rule(monkeypatch):
    """LLM 给出非法动作（维度不在候选内）-> 回落规则兜底，仍能下钻。"""
    from fakes import make_deps

    _patch_structured(
        monkeypatch,
        {
            "action": "drill",
            "parent_dim": "渠道",
            "value": "APP",
            "dim": "不存在的维度",
            "reason": "编造的维度",
        },
    )
    deps = make_deps(get_chat_model=lambda: _FakeLLM(None))
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    drilldown = result["diagnosis"].get("drilldown")
    assert drilldown and drilldown[0]["dim"] == "品类"  # 规则兜底的候选维度


# ---------------- 行动建议召回 ----------------


class _FakeRetriever:
    def __init__(self, docs):
        self.docs = docs
        self.queries = []

    async def retrieve(self, query, tenant_id=None, owner_id=None):
        self.queries.append(query)
        return self.docs


def test_diagnose_advice_docs_injected():
    """知识库命中 -> advice_docs 注入答复上下文。"""
    from fakes import make_deps

    deps = make_deps(
        retriever=_FakeRetriever(
            [{"title": "GMV口径", "content": "GMV=支付金额", "source": "口径字典"}]
        )
    )
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    advice = result["diagnosis"].get("advice_docs")
    assert advice and advice[0]["content"].startswith("GMV=支付金额")
    assert deps.retriever.queries  # 确实发起了召回


def test_diagnose_advice_docs_skipped_without_retriever():
    from fakes import make_deps

    deps = make_deps(retriever=None)
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    result = asyncio.run(node(state))
    assert not result["diagnosis"].get("advice_docs")
    # 模板建议仍在（降级不丢结论）
    assert result["diagnosis"].get("action_hints")


def test_diagnose_advice_docs_permission_denied_keeps_hints():
    """无 knowledge 权限 -> harness 拦截召回，保留模板建议。"""
    from dmf_agent.config.settings import HarnessSettings
    from dmf_agent.harness import HarnessController, build_default_tool_registry
    from fakes import make_deps

    harness = HarnessController(
        HarnessSettings(enabled=True), registry=build_default_tool_registry()
    )
    deps = make_deps(
        harness=harness,
        retriever=_FakeRetriever([{"title": "x", "content": "y"}]),
    )
    node, state = _node_and_state(deps, ["渠道", "地区"], _significant_qr())
    state["user_permissions"] = ["semantic:query"]  # 缺 knowledge
    result = asyncio.run(node(state))
    diag = result["diagnosis"]
    assert not diag.get("advice_docs")
    assert diag.get("action_hints")


# ---------------- 确定性 ----------------


def test_tie_break_is_deterministic():
    """并列变化量的维值顺序必须稳定（set 迭代顺序曾导致 TOP 漂移）。"""
    from dmf_agent.diagnosis.core import period_dim_attribution

    rows = [
        {"统计日期": "2026-08-01", "门店": "B店", "GMV": 100},
        {"统计日期": "2026-08-01", "门店": "A店", "GMV": 100},
        {"统计日期": "2026-08-02", "门店": "B店", "GMV": 100},
        {"统计日期": "2026-08-02", "门店": "A店", "GMV": 100},
        {"统计日期": "2026-08-03", "门店": "B店", "GMV": 200},
        {"统计日期": "2026-08-03", "门店": "A店", "GMV": 200},
        {"统计日期": "2026-08-04", "门店": "B店", "GMV": 200},
        {"统计日期": "2026-08-04", "门店": "A店", "GMV": 200},
    ]
    orders = set()
    for _ in range(5):
        res = period_dim_attribution(rows, "统计日期", "门店", "GMV")
        orders.add(tuple(r["dim_value"] for r in res))
    assert len(orders) == 1  # 多次运行顺序一致
