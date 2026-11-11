"""过滤候选召回测试（纯 Python，合成维值数据，不依赖真实 HM）。"""

from dmf_agent.config.settings import RecallSettings
from dmf_agent.recall.filter import FilterRecaller, _is_all_symbols


# ---- _is_all_symbols ----


def test_is_all_symbols():
    assert _is_all_symbols("---") is True
    assert _is_all_symbols("!@#") is True
    assert _is_all_symbols("GMV-华东") is False
    assert _is_all_symbols("") is False


# ---- FilterRecaller 基本打分 ----


def test_recall_exact_value_prepends_dim_name():
    # 2 字符维值拼接主维度名（get_primary_name 去括号后），mapping 完全命中
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市", "dim_value": "上海", "match": False}]
    result = recaller.recall("城市为上海", rows)
    assert [c.dim_value for c in result.candidates] == ["城市上海"]
    assert result.dim_values == ["城市上海"]
    assert result.candidates[0].mapping == 1.0
    assert result.candidates[0].original_dim_value == "上海"


def test_recall_match_rate_doubles():
    # HM match=True 时 rate=2，mapping 翻倍
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市", "dim_value": "上海", "match": True}]
    result = recaller.recall("城市为上海", rows)
    assert result.candidates[0].mapping == 2.0
    assert result.candidates[0].rate == 2


def test_recall_two_char_no_match_dropped():
    # 原维值 == 2 字符且 query 完全未命中 -> 舍弃
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市", "dim_value": "北京", "match": False}]
    result = recaller.recall("城市为上海", rows)
    assert result.candidates == []
    assert result.dim_values == []


def test_recall_pure_symbol_dropped():
    recaller = FilterRecaller()
    rows = [
        {"dim_id": 66, "dim_name": "城市", "dim_value": "---", "match": False},
        {"dim_id": 66, "dim_name": "城市", "dim_value": "上海", "match": False},
    ]
    result = recaller.recall("城市为上海", rows)
    assert "---" not in result.dim_values


def test_recall_removes_shifou():
    # 维值中的"是否"会被去除后再参与 mapping
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市", "dim_value": "是否上海", "match": False}]
    result = recaller.recall("上海", rows)
    assert [c.dim_value for c in result.candidates] == ["上海"]
    assert result.candidates[0].original_dim_value == "是否上海"


def test_recall_filters_kv_grouping():
    # 同维度多个维值 -> 按 dimension 分组、dimension_value 列表
    recaller = FilterRecaller()
    rows = [
        {"dim_id": 66, "dim_name": "渠道", "dim_value": "线上", "match": False},
        {"dim_id": 66, "dim_name": "渠道", "dim_value": "门店", "match": False},
    ]
    result = recaller.recall("线上门店渠道", rows)
    # 同 mapping 时按 original_dim_value 降序（老 polars 同款字符串排序），保序分组
    assert result.filters == [
        {"dimension": "渠道", "dimension_value": ["门店", "线上"]}
    ]


def test_recall_entities_exactly_channel():
    # entities 精确通道：mapping 0.5 但 exactly_mapping 1.0，高阈值下仍召回
    recaller = FilterRecaller(RecallSettings(filter_min_value=0.9))
    rows = [{"dim_id": 67, "dim_name": "地区", "dim_value": "华东大区", "match": False}]
    result = recaller.recall("看下华东的数据", rows, entities=["华东"])
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.mapping == 0.5
    assert cand.exactly_mapping == 1.0
    assert result.filters == [{"dimension": "地区", "dimension_value": ["华东大区"]}]


def test_recall_custom_threshold_excludes_low_score():
    # filter_min_value=0.8 时 mapping=0.5 的候选被排除
    recaller = FilterRecaller(RecallSettings(filter_min_value=0.8))
    rows = [{"dim_id": 67, "dim_name": "地区", "dim_value": "上海地区", "match": False}]
    result = recaller.recall("城市为上海", rows)
    assert result.candidates == []


def test_recall_empty_query():
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市", "dim_value": "上海", "match": False}]
    result = recaller.recall("", rows)
    assert result.candidates == []
    assert result.filters == []


def test_recall_no_rows():
    recaller = FilterRecaller()
    result = recaller.recall("城市为上海", [])
    assert result.candidates == []
    assert result.dim_values == []
    assert result.filters == []


def test_recall_normalizes_hm_keys():
    # 兼容 HM getByValue 原始键（dimId/dimCnName/dimValue/match）
    recaller = FilterRecaller()
    rows = [
        {
            "dimId": 66,
            "dimCnName": "城市",
            "dimValue": "上海",
            "dimSimilarValue": "common",
            "match": False,
        }
    ]
    result = recaller.recall("城市为上海", rows)
    assert [c.dim_value for c in result.candidates] == ["城市上海"]
    assert result.candidates[0].dim_id == 66


def test_recall_primary_name_strips_parentheses_in_prefix():
    # 拼接维度名前缀时用去括号后的主维度名
    recaller = FilterRecaller()
    rows = [{"dim_id": 66, "dim_name": "城市（一级）", "dim_value": "上海", "match": False}]
    result = recaller.recall("城市为上海", rows)
    assert [c.dim_value for c in result.candidates] == ["城市上海"]


def test_recall_high_mapping_sorted_first():
    # 多候选按 mapping 降序（完全命中排前面）
    recaller = FilterRecaller()
    rows = [
        {"dim_id": 66, "dim_name": "城市", "dim_value": "上海市", "match": False},
        {"dim_id": 66, "dim_name": "城市", "dim_value": "上海", "match": False},
    ]
    result = recaller.recall("城市为上海", rows)
    # "上海" 拼接后 mapping=1.0 排第一；"上海市" mapping=2/3
    assert result.candidates[0].dim_value == "城市上海"
    assert result.candidates[1].dim_value == "上海市"
    assert result.candidates[0].mapping > result.candidates[1].mapping


# ---- 规则降级抽取的过滤链路集成（假语义引擎供维值，无网络） ----


class _FakeDimEngine:
    """只提供 dimension_values 的极简替身（过滤召回数据源）。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def dimension_values(self, dim_name, limit=None):
        self.calls.append(dim_name)
        return self.rows


async def test_rule_based_extract_filters_via_dim_values():
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
    from dmf_agent.config.settings import Settings, StarRocksSettings
    from dmf_agent.semantic.layer import SemanticLayer

    layer = SemanticLayer()
    engine = _FakeDimEngine(["华东", "华南"])
    deps = AgentDeps(
        settings=Settings(starrocks=StarRocksSettings(enable_dim_value_recall=True)),
        get_chat_model=lambda: None,
        semantic_layer=layer,
        metric_engine=engine,
        filter_recaller=FilterRecaller(),
    )
    result = await _rule_based_extract("地区为华东", deps)
    # 未命中（华南，2 字符且未完整出现在 query）被过滤，仅华东进入 filters
    assert result["filters"] == [{"dimension": "地区", "dimension_value": ["华东"]}]
    # 维值反查确实走了语义引擎（维度 = query 中已识别的维度）
    assert engine.calls == ["地区"]


async def test_rule_based_extract_filters_empty_without_engine():
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
    from dmf_agent.config.settings import Settings
    from dmf_agent.semantic.layer import SemanticLayer

    deps = AgentDeps(
        settings=Settings(),
        get_chat_model=lambda: None,
        semantic_layer=SemanticLayer(),
        metric_engine=None,  # 语义引擎未装配：无维值数据源，filters 降级为空
        filter_recaller=FilterRecaller(),
    )
    result = await _rule_based_extract("地区为华东", deps)
    assert result["filters"] == []


async def test_rule_based_extract_filters_empty_when_recall_disabled():
    """维值反查开关关闭（默认）时不触发任何 StarRocks 查询。"""
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
    from dmf_agent.config.settings import Settings
    from dmf_agent.semantic.layer import SemanticLayer

    engine = _FakeDimEngine(["华东"])
    deps = AgentDeps(
        settings=Settings(),  # enable_dim_value_recall 默认 False
        get_chat_model=lambda: None,
        semantic_layer=SemanticLayer(),
        metric_engine=engine,
        filter_recaller=FilterRecaller(),
    )
    result = await _rule_based_extract("地区为华东", deps)
    assert result["filters"] == []
    assert engine.calls == []
