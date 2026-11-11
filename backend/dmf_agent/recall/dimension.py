"""维度候选召回（迁移自老系统 candidates_dim.py 的 get_dim_score）。

老系统链路：HmApiMeta.get_all_dims_object 取全量维度 -> polars 向量化
打分（get_primary_name 去括号 -> get_common_str_by_interval 算 mapping ->
is_substr 算 exactly_mapping -> 阈值过滤 -> 排序 -> limit）。

v2 拆为纯函数 + DimensionRecaller，输入维度名集合（来自指标元数据的
dimensions 字段或 HM 维度元数据），输出结构化结果：

- dimensions           : 召回的维度名（唯一、按分排序）
- chat_map_names       : mapping < 1 的模糊命中，交 LLM 二次确认
- exact_mapping        : 实体子串/字符重叠完全命中的维度
- string_mapping_high  : 0.5 <= mapping < 1 的高分字符匹配

entities（query 中已识别的实体名）来自 LLM 实体抽取，mock 模式下为空，
此时 is_substr 通道自然失效（退化，不报错），召回仅由 mapping 通道驱动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from dmf_agent.config.settings import RecallSettings
from dmf_agent.recall.common_str import get_common_str_by_interval


def get_primary_name(name: str) -> str:
    """去掉指标/维度名括号内容（迁移自老系统 get_primary_name.py）。

    例："渠道（一级）" -> "渠道"；"城市（新）" -> "城市"。
    """
    if name:
        name = name.split("(")[0].split("（")[0]
    return name


def is_substr(entities: List[str], short: str) -> Optional[str]:
    """实体与候选名的子串/字符重叠匹配（迁移自老系统 is_substr）。

    任一实体与候选名互为子串 -> 返回候选名本身（完全命中）；
    否则返回共享字符最多的重叠串（长度用于 exactly_mapping 打分），
    无重叠返回 None。
    """
    if not entities or not short:
        return None
    for entity in entities:
        if len(entity) > len(short):
            _query, _short = short, entity
        else:
            _query, _short = entity, short
        if _query.lower() in _short.lower():
            return short
    match_dict: dict = {}
    for entity in entities:
        if len(entity) > len(short):
            _query, _short = short, entity
        else:
            _query, _short = entity, short
        common_str = "".join(set(_query) & set(_short))
        if common_str:
            match_dict[common_str] = len(common_str)
    if match_dict:
        return max(match_dict, key=match_dict.get)
    return None


@dataclass
class DimensionRecallResult:
    dimensions: List[str] = field(default_factory=list)
    chat_map_names: List[str] = field(default_factory=list)
    exact_mapping: List[str] = field(default_factory=list)
    string_mapping_high: List[str] = field(default_factory=list)


class DimensionRecaller:
    def __init__(self, settings: Optional[RecallSettings] = None) -> None:
        self._settings = settings or RecallSettings()

    def recall(
        self, query: str, dim_names: List[str], entities: Optional[List[str]] = None
    ) -> DimensionRecallResult:
        """对维度名集合做容错子序列 + 实体子串召回，返回结构化结果。"""
        if not query or not dim_names:
            return DimensionRecallResult()
        entities = entities or []
        min_value = self._settings.dim_min_value
        rows = []
        for name in dict.fromkeys(dim_names):
            primary = get_primary_name(name)
            if not primary:
                continue
            common = get_common_str_by_interval(query, primary, self._settings.interval)
            exactly = is_substr(entities, primary)
            mapping = len(common) / len(primary)
            exactly_mapping = (len(exactly) / len(primary)) if exactly else None

            # 长度 <=2 的维度名若非完全命中则置 0（走完全匹配通道）
            if len(primary) <= 2 and mapping < 1:
                mapping = 0.0
            if mapping == 0:
                continue
            # 阈值过滤：mapping 或 exactly_mapping 任一达标
            if not (mapping >= min_value or (exactly_mapping is not None and exactly_mapping >= min_value)):
                continue
            # 单字公共子序列仅当维度名本身为单字时保留
            if not (len(common) > 1 or len(primary) == 1):
                continue
            rows.append((name, mapping, exactly_mapping or 0.0, common))

        # 排序：mapping 降序、exactly_mapping 降序、common 长度降序、名称升序
        rows.sort(key=lambda r: (-r[1], -r[2], -len(r[3]), r[0]))
        rows = rows[: self._settings.dim_max_nums]
        return DimensionRecallResult(
            dimensions=[r[0] for r in rows],
            chat_map_names=[r[0] for r in rows if r[1] < 1],
            exact_mapping=[r[0] for r in rows if r[2] >= 1],
            string_mapping_high=[r[0] for r in rows if 0.5 <= r[1] < 1],
        )
