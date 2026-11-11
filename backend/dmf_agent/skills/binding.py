"""Skill 输入绑定：把 skill 声明的 `inputs` 按名绑到已抽取的 `query_elements`。

对话里没有「填表」这一步——用户在聊天里自由表达，要素由 `extract_elements` 节点
抽取成 `query_elements`（`metrics` / `dimensions` / `filters` / `time_range`）。
本模块把 skill 的输入名映射到这些要素上，绑不到的留空，交给
`executor.SkillExecutor` 的 `validate_inputs` 报「缺少必填输入」，
再由澄清追问链路补齐。

纯函数、无 IO，可脱离图单测。
"""

from __future__ import annotations

from typing import Any, Dict

from dmf_agent.skills.package import SkillPackage

# 输入名 → query_elements 键。左列是 skill 作者可能写的名字（含常见同义词）。
_ELEMENT_ALIASES: Dict[str, str] = {
    "metric": "metrics",
    "metrics": "metrics",
    "dimension": "dimensions",
    "dimensions": "dimensions",
    "time": "time_range",
    "days": "time_range",
    "date": "time_range",
    "time_range": "time_range",
    "filter": "filters",
    "filters": "filters",
}

# 取「整个列表」而非首个值的输入名（复数形式）；单数（metric/dimension）是标量语义
_LIST_INPUTS = frozenset({"metrics", "dimensions", "filters"})


def _format_time_range(value: Any) -> Any:
    """把 time_range 渲染成 skill 模板好用的字符串（`2026-08-01~2026-08-07:day`）。"""
    if not isinstance(value, dict):
        return None
    start, end = value.get("start"), value.get("end")
    if not start or not end:
        return None  # 缺口的时间区间宁可不绑（交由校验报缺失），不传半截结构
    granularity = value.get("granularity") or value.get("grain")
    return f"{start}~{end}:{granularity}" if granularity else f"{start}~{end}"


def bind_inputs(pkg: SkillPackage, elements: Dict[str, Any]) -> Dict[str, Any]:
    """按 skill.inputs 的名字从 elements 取值；绑不到的**不放进结果**（交由校验报缺失）。"""
    source = elements or {}
    bound: Dict[str, Any] = {}
    for spec in pkg.inputs:
        key = _ELEMENT_ALIASES.get(spec.name, spec.name)
        if key not in source:
            continue
        if key == "time_range":
            value: Any = _format_time_range(source.get(key))
        elif spec.name in _LIST_INPUTS:
            value = source.get(key)  # 复数输入名绑定整个列表
        else:
            value = source.get(key)  # 标量语义：列表取首值，空列表视为未绑定
            if isinstance(value, list):
                if not value:
                    continue
                value = value[0]
        if value in (None, ""):
            continue
        bound[spec.name] = value
    return bound
