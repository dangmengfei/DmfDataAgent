"""空数据时间窗重定向澄清（FR5 闭环）。

- 纯函数 compute_time_redirect：单日 / 周期截断 / 区间平移 / 整窗未来 四种窗口形态；
- diagnose_empty：时间越界附 redirect；已重定向过一次不再下发（防循环）；
- apply_clarification.merge_selection：time_redirect 分支沿用基线要素、只换时间窗；
- 端到端：首轮空数据 -> final 带 clarify_time_redirect 卡片；用户确认后续跑取到数。
"""

from datetime import date

from dmf_agent.agents.metric_agent import build_metric_agent_graph
from dmf_agent.agents.metric_agent.empty_trace import diagnose_empty
from dmf_agent.agents.metric_agent.empty_trace import compute_time_redirect
from dmf_agent.config.settings import ObservabilitySettings, Settings
from dmf_agent.db.memory import InMemoryMessageRepository, InMemorySessionRepository
from dmf_agent.semantic.layer import SemanticLayer
from dmf_agent.services.chat_service import ChatService
from fakes import FakeMetricEngine, make_deps

AS_OF = "2026-10-15"


# ---------- 纯函数：窗口智能调整 ----------

def test_redirect_span_preserves_length():
    """近 7 天（10-22~10-28）晚于截至日 10-15 -> 整体平移保跨度：10-09~10-15。"""
    tr = {"start": "2026-10-22", "end": "2026-10-28", "granularity": "day"}
    r = compute_time_redirect(tr, AS_OF)
    assert r is not None
    assert r["window_kind"] == "span"
    assert r["adjusted"] == {"start": "2026-10-09", "end": AS_OF, "granularity": "day"}
    assert r["data_as_of"] == AS_OF


def test_redirect_period_truncates_keeps_start():
    """本月（10-01~10-28）-> 保留起点、截到截至日：10-01~10-15。"""
    tr = {"start": "2026-10-01", "end": "2026-10-28", "granularity": "day"}
    r = compute_time_redirect(tr, AS_OF)
    assert r["window_kind"] == "period"
    assert r["adjusted"] == {"start": "2026-10-01", "end": AS_OF, "granularity": "day"}


def test_redirect_single_day_to_as_of():
    tr = {"start": "2026-10-20", "end": "2026-10-20", "granularity": "day"}
    r = compute_time_redirect(tr, AS_OF)
    assert r["window_kind"] == "single"
    assert r["adjusted"] == {"start": AS_OF, "end": AS_OF, "granularity": "day"}


def test_redirect_entire_window_in_future():
    tr = {"start": "2026-11-01", "end": "2026-11-30", "granularity": "day"}
    r = compute_time_redirect(tr, AS_OF)
    assert r["window_kind"] == "future"
    assert r["adjusted"]["start"] == AS_OF and r["adjusted"]["end"] == AS_OF


def test_redirect_not_applicable():
    # end 未晚于截至日
    assert compute_time_redirect(
        {"start": "2026-10-01", "end": "2026-10-10", "granularity": "day"}, AS_OF
    ) is None
    # 无截至日 / 无 end
    assert compute_time_redirect({"start": "2026-10-01", "end": "2026-10-28"}, None) is None
    assert compute_time_redirect({"start": "2026-10-01"}, AS_OF) is None


# ---------- diagnose_empty：redirect 装配与防循环 ----------

async def test_diagnose_attaches_redirect_and_respects_flag():
    qr = {"columns": ["统计日期", "GMV"], "rows": [], "row_count": 0, "models": []}
    elements = {"time_range": {"start": "2026-10-22", "end": "2026-10-28", "granularity": "day"}}

    class _Engine:
        async def data_as_of(self, models):
            return AS_OF

    diag = await diagnose_empty(
        query_result=qr, query_elements=elements, dsl={}, engine=_Engine(),
        today=date(2026, 10, 28),
    )
    assert diag.is_empty and diag.redirect
    assert diag.redirect["adjusted"]["end"] == AS_OF

    # 已重定向过一次仍为空 -> 不再下发
    diag2 = await diagnose_empty(
        query_result=qr, query_elements=elements, dsl={}, engine=_Engine(),
        today=date(2026, 10, 28), already_redirected=True,
    )
    assert diag2.redirect is None


# ---------- merge_selection：确认续跑只换时间窗 ----------

def test_merge_selection_time_redirect_preserves_base_elements():
    from dmf_agent.agents.metric_agent.nodes.apply_clarification import merge_selection

    base = {
        "metrics": ["GMV"],
        "dimensions": ["渠道"],
        "filters": [],
        "time_range": {"start": "2026-10-22", "end": "2026-10-28", "granularity": "day"},
        "time_redirected": True,
    }
    selection = {
        "time_redirect": "latest",
        "time_range": {"start": "2026-10-09", "end": AS_OF, "granularity": "day"},
    }
    out = merge_selection(base, selection, make_deps())
    assert out["metrics"] == ["GMV"]          # 基线要素保留
    assert out["dimensions"] == ["渠道"]
    assert out["time_range"]["end"] == AS_OF  # 时间窗已换
    assert out["time_redirected"] is True
    assert out["confidence"] == 1.0


# ---------- 端到端：空数据 -> 卡片 -> 确认后续跑取数 ----------

def _service(engine):
    layer = SemanticLayer()
    deps = make_deps(layer=layer, engine=engine)
    settings = Settings(observability=ObservabilitySettings(enabled=False))
    deps.settings = settings
    return ChatService(
        graph=build_metric_agent_graph(deps),
        session_repo=InMemorySessionRepository(),
        message_repo=InMemoryMessageRepository(),
        settings=settings,
    ), engine


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
        if ev.get("type") in ("final", "error"):
            break
    return events


async def test_e2e_empty_then_confirm_redirect_queries_latest():
    engine = FakeMetricEngine(layer=SemanticLayer(), empty=True, data_as_of_date=AS_OF)
    svc, engine = _service(engine)

    ev1 = await _collect(
        svc.astream_chat(session_id=None, user_id="u1", message="查一下近7天的GMV趋势")
    )
    final1 = ev1[-1]
    assert final1["type"] == "final", final1
    clarification = final1.get("clarification")
    assert clarification is not None, "空数据应下发重定向澄清卡片"
    assert clarification["type"] == "clarify_time_redirect"
    adjusted = clarification["adjusted"]
    assert adjusted["end"] == AS_OF
    sid = final1["session_id"]

    # 用户确认 -> 带 clarify_selection 续跑
    selection = {
        "time_redirect": "latest",
        "time_range": adjusted,
        "context": clarification["context"],
    }
    ev2 = await _collect(
        svc.astream_chat(
            session_id=sid, user_id="u1",
            message=f"改查最新数据（截至 {AS_OF}）",
            clarify_selection=selection,
        )
    )
    final2 = ev2[-1]
    assert final2["type"] == "final", final2
    assert final2.get("clarification") is None, "续跑成功后不应再挂确认"
    # 引擎确实按调整后窗口取数，且取到了行
    last_dsl = engine.queries[-1]
    assert last_dsl["time_range"]["end"] == AS_OF
    assert last_dsl["time_range"]["start"] == adjusted["start"]


async def test_e2e_empty_for_other_reason_no_redirect_card():
    """空数据但时间窗未越界（如其它条件导致）-> 不下发重定向卡片。"""

    class _AlwaysEmpty(FakeMetricEngine):
        def _is_beyond_as_of(self, source):
            return True  # 无论时间窗都空

    # data_as_of 在未来：时间原因不成立
    engine = _AlwaysEmpty(layer=SemanticLayer(), empty=True, data_as_of_date="2026-12-31")
    svc, _ = _service(engine)
    ev = await _collect(
        svc.astream_chat(session_id=None, user_id="u2", message="查一下近7天的GMV趋势")
    )
    final = ev[-1]
    assert final["type"] == "final"
    assert final.get("clarification") is None


# ---------- 回归：聚合查询的「一行全 NULL」不是有数据 ----------

def test_is_effectively_empty_treats_all_null_row_as_empty():
    """真实 StarRocks：无 GROUP BY 的聚合在无匹配行时返回 row_count=1, rows=[[None]]。

    只判 row_count != 0 会把「该时间窗没有数据」误判成有结果，FR5 卡片永不触发。
    """
    from dmf_agent.agents.metric_agent.empty_trace import is_effectively_empty

    assert is_effectively_empty({"row_count": 1, "rows": [[None]]}) is True
    assert is_effectively_empty({"row_count": 0, "rows": []}) is True
    # 有真实数值 / 有维度行 -> 非空
    assert is_effectively_empty({"row_count": 1, "rows": [[360.0]]}) is False
    assert is_effectively_empty({"row_count": 1, "rows": [["2026-10-09", None]]}) is False
    # 空表契约（无 rows 键）按空处理
    assert is_effectively_empty({"row_count": 0}) is True


async def test_empty_aggregate_null_row_triggers_redirect_card():
    """端到端：纯聚合（无维度）时间窗越界 -> 引擎只给一行全 NULL，仍应下发重定向卡片。"""
    engine = FakeMetricEngine(layer=SemanticLayer(), empty=True, data_as_of_date=AS_OF)
    # 先固定形状：确认 fake 现在复刻真实引擎的 NULL 行（而非 0 行）
    dsl = {
        "metrics": [{"name": "GMV"}],
        "dimensions": [],
        "filters": [],
        "time_range": {"start": "2026-10-22", "end": "2026-10-28", "granularity": "day"},
    }
    res = await engine.query(dsl)
    assert res["row_count"] == 1 and res["rows"] == [[None]], res

    svc, _ = _service(engine)
    ev1 = await _collect(svc.astream_chat(session_id=None, user_id="u1", message="近7天的GMV是多少"))
    final = ev1[-1]
    assert final["type"] == "final"
    assert final.get("clarification") is not None, "一行全 NULL 应触发重定向澄清"
    assert final["clarification"]["type"] == "clarify_time_redirect"
    assert final["clarification"]["adjusted"]["end"] == AS_OF
