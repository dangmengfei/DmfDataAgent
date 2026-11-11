"""过滤候选召回（candidates_filter 迁移，Phase 2.4b）。

迁移自老系统 plugin/get_indicators/get_element/candidates_filter.py：
- 数据源：HM API v1/standard-api/sa/dim/getByValue 维值反查
  （见 metadata/client.py::HmMetaClient.fetch_dim_values），行字段
  dimId/dimCnName/dimValue/match（dimSimilarValue 老实现中参与运算前即被
  覆盖，v2 不再透传）
- 打分：get_filter_score（mapping / exactly_mapping 双通道 + 维值预处理）

纯 Python 重写（老实现为 Polars 向量化），语义逐条对齐：
- 纯符号维值丢弃（老 udf_isalnum 反义过滤）
- 原维值 <= 2 字符时拼接主维度名前缀（get_primary_name 去括号后）；
  原维值 == 2 字符且 query 未命中则直接舍弃
- 去除维值中的 "是否"
- mapping = len(common) / len(dim_value) * rate（HM match 命中 rate=2，否则 1）
- dim_value <= 2 字符走完全匹配（mapping<1 置 0）
- 阈值过滤（filter_min_value）+ 数量截断（filter_max_nums）
- 输出：candidates / dim_values / filters（按维度名分组的 KV 格式）

与 dimension.py 一致的有意偏差：common 排序用长度而非字符串字典序。
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dmf_agent.config.settings import RecallSettings
from dmf_agent.recall.common_str import get_common_str_by_interval
from dmf_agent.recall.dimension import get_primary_name, is_substr

_FIELD_ALIASES = {
    "dim_id": ("dim_id", "dimId"),
    "dim_name": ("dim_name", "dimCnName"),
    "dim_value": ("dim_value", "dimValue"),
    "match": ("match",),
}


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 HM getByValue 返回行规整为 v2 命名（兼容原始键与已规整键）。"""
    out: Dict[str, Any] = {}
    for key, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in row:
                out[key] = row[alias]
                break
    return out


def _is_all_symbols(s: str) -> bool:
    """判断是否为纯符号串（老 udf_isalnum：字符集合是 string.punctuation 子集且非空）。"""
    if not isinstance(s, str) or not s:
        return False
    s_set = set(s)
    return s_set.issubset(set(string.punctuation)) and len(s_set) > 0


@dataclass
class FilterCandidate:
    dim_id: Any
    dim_name: str
    original_dim_value: str  # 原始维值（参与 exactly 通道）
    dim_value: str  # 处理后的维值（可能拼接维度名前缀 / 去"是否"）
    common: str  # query 与处理前维值的容错公共子序列（filter 过滤用）
    mapping: float
    exactly_mapping: float  # 无精确匹配为 0.0
    match: bool = False
    rate: int = 1


@dataclass
class FilterRecallResult:
    candidates: List[FilterCandidate] = field(default_factory=list)
    # 处理后的维值列表（老 get_filter_candidates 返回 df["dim_value"].to_list()）
    dim_values: List[str] = field(default_factory=list)
    # KV 分组格式（老 filter_to_kv_list）：[{"dimension", "dimension_value": [...]}]
    filters: List[Dict[str, Any]] = field(default_factory=list)


class FilterRecaller:
    """过滤候选召回（迁移自老 candidates_filter.get_filter_score）。"""

    def __init__(self, settings: Optional[RecallSettings] = None) -> None:
        self._settings = settings or RecallSettings()

    def score(
        self,
        query: str,
        rows: List[Dict[str, Any]],
        entities: Optional[List[str]] = None,
    ) -> List[FilterCandidate]:
        """纯打分：输入维值候选行，输出按 mapping 降序、截断后的候选。

        rows 为字典列表，兼容 HM 原始键（dimId/dimCnName/dimValue/match）
        与 v2 规整键。无 query / 无行时返回空列表。
        """
        settings = self._settings
        entities = entities or []
        if not query or not rows:
            return []
        groups: Dict[tuple, FilterCandidate] = {}
        for raw in rows:
            row = _normalize_row(raw) if isinstance(raw, dict) else {}
            dim_id = row.get("dim_id")
            dim_name = str(row.get("dim_name") or "")
            original = str(row.get("dim_value") or "")
            match = bool(row.get("match"))
            # 纯符号 / 空维值直接丢弃（老 isalnum 过滤 + 后续 mapping==0 等价）
            if not original or _is_all_symbols(original):
                continue
            rate = 2 if match else 1
            original_common = get_common_str_by_interval(query, original)
            math_dim_name = get_primary_name(dim_name)
            # 新 dim_value：原值 <= 2 字符拼接主维度名；== 2 字符且未命中则舍弃
            dim_value = math_dim_name + original if len(original) <= 2 else original
            if len(original) == 2 and len(original_common) == 0:
                dim_value = ""
            dim_value = dim_value.replace("是否", "")
            if not dim_value:
                continue
            common = get_common_str_by_interval(query, dim_value)
            exactly_match = is_substr(entities, original)
            mapping = len(common) / len(dim_value) * rate
            exactly_mapping = (len(exactly_match) / len(original) * rate) if exactly_match else 0.0
            # <= 2 字符维值走完全匹配
            if len(dim_value) <= 2 and mapping < 1:
                mapping = 0.0
            if mapping == 0:
                continue
            if not (
                (mapping >= settings.filter_min_value or exactly_mapping >= settings.filter_min_value)
                and (len(common) > 1 or len(dim_value) == 1)
            ):
                continue
            cand = FilterCandidate(
                dim_id=dim_id,
                dim_name=dim_name,
                original_dim_value=original,
                dim_value=dim_value,
                common=common,
                mapping=mapping,
                exactly_mapping=exactly_mapping,
                match=match,
                rate=rate,
            )
            # 老 group_by(dim_id,dim_name,original_dim_value,dim_value,common,exactly_match)
            # + agg(max) —— 去重取高分
            key = (str(dim_id), dim_name, original, dim_value, common, exactly_match)
            prev = groups.get(key)
            if prev is None or cand.mapping > prev.mapping:
                groups[key] = cand
        scored = list(groups.values())
        # 稳定排序从低优先级到高优先级（主键最后一次生效）：
        # dim_name 升序 < original_dim_value 降序 < mapping/exactly_mapping/len(common) 降序
        scored.sort(key=lambda c: c.dim_name)
        scored.sort(key=lambda c: c.original_dim_value, reverse=True)
        scored.sort(key=lambda c: (-c.mapping, -c.exactly_mapping, -len(c.common)))
        return scored[: settings.filter_max_nums]

    def recall(
        self,
        query: str,
        rows: List[Dict[str, Any]],
        entities: Optional[List[str]] = None,
    ) -> FilterRecallResult:
        """打分并组装结果：candidates / dim_values / filters（KV 分组）。"""
        candidates = self.score(query, rows, entities)
        return FilterRecallResult(
            candidates=candidates,
            dim_values=[c.dim_value for c in candidates],
            filters=self._to_kv(candidates),
        )

    @staticmethod
    def _to_kv(candidates: List[FilterCandidate]) -> List[Dict[str, Any]]:
        """按维度名分组去重原始维值（老 filter_to_kv_list，v2 保序输出）。"""
        by_dim: Dict[str, List[str]] = {}
        for cand in candidates:
            values = by_dim.setdefault(cand.dim_name, [])
            if cand.original_dim_value not in values:
                values.append(cand.original_dim_value)
        return [
            {"dimension": dim, "dimension_value": values}
            for dim, values in by_dim.items()
        ]
