"""黑话维度/维值词典测试（CantDictionary，迁移自老 candidates_cant.py）。"""

from dmf_agent.integrations.metric_platform.client import _dedup_cant_domains
from dmf_agent.recall.black_talk import (
    CantDictionary,
    DEFAULT_CANT_DICT,
)


# ---- from_raw_rows（HM 原始行 -> 词典） ----


def test_from_raw_rows_dim():
    cant = CantDictionary.from_raw_rows(
        [{"cantType": "dim", "cnName": "渠道", "cantCnName": "渠道类型"}]
    )
    assert len(cant.entries) == 1
    assert cant.entries[0].cant_type == "dim"
    assert cant.entries[0].cn_name == "渠道"
    assert cant.entries[0].cant_cn_name == "渠道类型"


def test_from_raw_rows_dim_value_expands_kv():
    # dim_value 行按 dimValue 逗号展开为 "维度:维值"（每个维值一行）
    cant = CantDictionary.from_raw_rows(
        [{"cantType": "dim_value", "cnName": "地区", "dimValue": "华东,华南", "cantCnName": "大区"}]
    )
    assert {e.cn_name for e in cant.entries} == {"地区:华东", "地区:华南"}
    assert {e.cant_type for e in cant.entries} == {"dim_value"}


def test_from_raw_rows_splits_slangs_cross_product():
    # cant_cn_name 逗号分隔 -> 与 cn_name 叉积（老 split_and_stack + str.split）
    cant = CantDictionary.from_raw_rows(
        [
            {
                "cantType": "dim",
                "cnName": "渠道",
                "cantCnName": "渠道类型,卖场",
            }
        ]
    )
    assert {e.cant_cn_name for e in cant.entries} == {"渠道类型", "卖场"}
    assert all(e.cn_name == "渠道" for e in cant.entries)


def test_from_raw_rows_strips_slang_spaces():
    cant = CantDictionary.from_raw_rows(
        [{"cantType": "dim", "cnName": "地区", "cantCnName": "区 域"}]
    )
    assert cant.entries[0].cant_cn_name == "区域"


def test_from_raw_rows_skips_empty_fields():
    cant = CantDictionary.from_raw_rows(
        [
            {"cantType": "dim", "cnName": "", "cantCnName": "x"},
            {"cantType": "dim", "cnName": "渠道", "cantCnName": ""},
            {"cantType": "dim", "cnName": "地区", "cantCnName": "区域"},
        ]
    )
    assert len(cant.entries) == 1
    assert cant.entries[0].cn_name == "地区"


def test_from_raw_rows_accepts_v2_keys():
    cant = CantDictionary.from_raw_rows(
        [{"cant_type": "dim", "cn_name": "渠道", "cant_cn_name": "渠道类型"}]
    )
    assert cant.entries[0].cn_name == "渠道"


# ---- recall（黑话完全匹配） ----


def test_recall_dim_hit():
    cant = CantDictionary.from_raw_rows(DEFAULT_CANT_DICT)
    result = cant.recall("按渠道类型看GMV")
    assert result.dimensions == ["渠道"]
    assert result.dimensions_cant == ["渠道类型"]
    assert result.filters == []


def test_recall_dim_value_hit_parses_kv():
    cant = CantDictionary.from_raw_rows(DEFAULT_CANT_DICT)
    result = cant.recall("大区GMV")
    assert result.dimensions == []
    assert result.filters == [
        {"dimension": "地区", "dimension_value": ["华东"], "filter_operator": "in"},
        {"dimension": "地区", "dimension_value": ["华南"], "filter_operator": "in"},
    ]


def test_recall_no_hit():
    cant = CantDictionary.from_raw_rows(DEFAULT_CANT_DICT)
    result = cant.recall("本月GMV环比")
    assert result.dimensions == []
    assert result.filters == []


def test_recall_empty_query():
    cant = CantDictionary.from_raw_rows(DEFAULT_CANT_DICT)
    result = cant.recall("")
    assert result.dimensions == []
    assert result.filters == []


def test_recall_dim_value_non_kv_name_skipped():
    # dim_value 条目 cn_name 非 "K:V" 格式（老 check ":" in cn_name）
    cant = CantDictionary.from_raw_rows(
        [{"cantType": "dim_value", "cnName": "地区", "dimValue": "", "cantCnName": "大区"}]
    )
    result = cant.recall("大区GMV")
    assert result.filters == []


def test_recall_multiple_entries_parallel_lists():
    cant = CantDictionary.from_raw_rows(
        [
            {"cantType": "dim", "cnName": "渠道", "cantCnName": "渠道类型"},
            {"cantType": "dim", "cnName": "地区", "cantCnName": "区域"},
        ]
    )
    result = cant.recall("看区域和渠道类型的GMV")
    assert result.dimensions == ["渠道", "地区"]
    assert result.dimensions_cant == ["渠道类型", "区域"]


# ---- HM 客户端 dm/dc 去重 ----


def test_dedup_cant_domains_personal_wins():
    data = [
        {"cantDomain": "dm", "cantCnName": "大区", "cnName": "地区"},
        {"cantDomain": "dc", "cantCnName": "大区", "cnName": "区域"},
        {"cantDomain": "dm", "cantCnName": "卖场", "cnName": "渠道"},
    ]
    result = _dedup_cant_domains(data)
    # 同名"大区"个人(dc)优先，公司(dm)被剔除
    assert result == [
        {"cantDomain": "dm", "cantCnName": "卖场", "cnName": "渠道"},
        {"cantDomain": "dc", "cantCnName": "大区", "cnName": "区域"},
    ]


def test_dedup_cant_domains_no_conflict_keeps_all():
    data = [
        {"cantDomain": "dm", "cantCnName": "大区", "cnName": "地区"},
        {"cantDomain": "dc", "cantCnName": "卖场", "cnName": "渠道"},
    ]
    assert _dedup_cant_domains(data) == data


# ---- 规则降级抽取的黑话链路集成 ----


async def test_rule_based_extract_cant_dim_and_filter():
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
    from dmf_agent.config.settings import Settings

    deps = AgentDeps(
        settings=Settings(),
        get_chat_model=lambda: None,
        cant_dict=CantDictionary.from_raw_rows(DEFAULT_CANT_DICT),
    )
    result = await _rule_based_extract("按渠道类型看大区GMV", deps)
    assert "渠道" in result["dimensions"]
    assert result["filters"] == [
        {"dimension": "地区", "dimension_value": ["华东"], "filter_operator": "in"},
        {"dimension": "地区", "dimension_value": ["华南"], "filter_operator": "in"},
    ]


async def test_rule_based_extract_no_cant_no_effect():
    from dmf_agent.agents.metric_agent.deps import AgentDeps
    from dmf_agent.agents.metric_agent.nodes.extract_elements import _rule_based_extract
    from dmf_agent.config.settings import Settings

    deps = AgentDeps(settings=Settings(), get_chat_model=lambda: None, cant_dict=None)
    result = await _rule_based_extract("按渠道类型看大区GMV", deps)
    assert result["dimensions"] == []
    assert result["filters"] == []
