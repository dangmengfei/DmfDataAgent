"""Skill 输入绑定测试（skills/binding.py：query_elements -> skill.inputs）。

纯函数测试，无 LLM / 无 DB / 无 StarRocks，结果确定。
"""

from __future__ import annotations

from dmf_agent.skills.binding import bind_inputs
from dmf_agent.skills.package import SkillInput, SkillPackage


def _pkg(*input_names: str) -> SkillPackage:
    return SkillPackage(
        name="t",
        inputs=[SkillInput(name=n) for n in input_names],
    )


_ELEMENTS = {
    "metrics": ["零售存款余额", "GMV"],
    "dimensions": ["机构", "渠道"],
    "filters": [{"dimension": "机构", "dimension_value": ["北京分行"]}],
    "time_range": {
        "start": "2026-08-01",
        "end": "2026-08-07",
        "granularity": "day",
    },
}


def test_alias_metric_takes_first_value():
    """metric 是标量语义：取 metrics 首值（${metric} 模板占位符要的是单个名字）。"""
    bound = bind_inputs(_pkg("metric"), _ELEMENTS)
    assert bound == {"metric": "零售存款余额"}


def test_alias_metrics_keeps_whole_list():
    """metrics（复数）绑定整个列表。"""
    bound = bind_inputs(_pkg("metrics"), _ELEMENTS)
    assert bound == {"metrics": ["零售存款余额", "GMV"]}


def test_alias_time_formats_range_string():
    """time -> time_range：渲染成 skill 模板好用的 `start~end:granularity`。"""
    bound = bind_inputs(_pkg("time"), _ELEMENTS)
    assert bound == {"time": "2026-08-01~2026-08-07:day"}


def test_alias_days_and_date_map_to_time_range():
    """days / date 都是 time_range 的同义词。"""
    for name in ("days", "date", "time_range"):
        assert bind_inputs(_pkg(name), _ELEMENTS) == {name: "2026-08-01~2026-08-07:day"}


def test_time_range_without_granularity():
    """无 granularity 时输出 `start~end`（不带尾缀）。"""
    elements = {"time_range": {"start": "2026-08-01", "end": "2026-08-07"}}
    assert bind_inputs(_pkg("time"), elements) == {"time": "2026-08-01~2026-08-07"}


def test_time_range_incomplete_not_bound():
    """time_range 缺 start/end 时整体不绑定（宁缺勿错，交由校验报缺失）。"""
    elements = {"time_range": {"start": "2026-08-01"}}
    assert bind_inputs(_pkg("time"), elements) == {}


def test_alias_dimension_and_dimensions():
    """dimension 取首值（标量）、dimensions 取整个列表。"""
    bound = bind_inputs(_pkg("dimension", "dimensions"), _ELEMENTS)
    assert bound == {"dimension": "机构", "dimensions": ["机构", "渠道"]}


def test_filters_bind_whole_list():
    """filters（复数）绑整个列表；filter（单数）与其余单数别名一致——取首个（单个条件）。"""
    bound = bind_inputs(_pkg("filters", "filter"), _ELEMENTS)
    assert bound["filters"] == _ELEMENTS["filters"]
    assert bound["filter"] == _ELEMENTS["filters"][0]


def test_unknown_name_falls_back_to_elements_key():
    """名字不在别名表里：直接按原名从 elements 取（自定义要素名兜底）。"""
    bound = bind_inputs(_pkg("order_by"), {"order_by": "GMV desc"})
    assert bound == {"order_by": "GMV desc"}


def test_empty_list_not_bound():
    """空列表视为未绑定（取首值会越界，干脆不绑，让校验报缺失）。"""
    bound = bind_inputs(_pkg("metric"), {"metrics": []})
    assert bound == {}


def test_empty_elements_binds_nothing():
    assert bind_inputs(_pkg("metric", "time"), {}) == {}


def test_empty_and_none_values_skipped():
    """绑到的值为 None / 空串时不注入（必填缺失由 executor 统一报）。"""
    bound = bind_inputs(_pkg("metric", "order_by"), {"metric": "", "order_by": None})
    assert bound == {}


def test_only_declared_inputs_bound():
    """只绑定 skill 声明过的 inputs，elements 里的其它要素不动。"""
    pkg = _pkg("metric")
    bound = bind_inputs(pkg, _ELEMENTS)
    assert set(bound.keys()) == {"metric"}
