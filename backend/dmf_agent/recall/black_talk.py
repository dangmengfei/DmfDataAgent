"""黑话词典（候选召回 black_talk / cant 通道）。

老系统黑话存于 MySQL cant 表（CantFuncs），按 tenant 读取；
v2 先用配置驱动（内置示例词典 + 可替换/扩展），后续接 MySQL 或文件配置。

两套词典：
- BlackTalkDictionary（指标黑话）：用户口语/俗称（如"成交额"）-> 标准指标名（"GMV"），
  供 MetricRecaller black_talk 通道使用（见 recall.py）
- CantDictionary（维度/维值黑话，迁移自老 candidates_cant.py）：cant_type=dim / dim_value，
  如用户说"大区"时等价于过滤"地区 in 华东"；走完全匹配，不交给 LLM 猜

黑话命中权重最高（score=1.0），且走完全匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class BlackTalkDictionary:
    """黑话词典：俗称/黑话 -> 标准指标名。可替换为从 MySQL/配置加载的实现。"""

    mapping: Dict[str, str] = field(default_factory=dict)

    def lookup(self, query: str) -> List[Tuple[str, str]]:
        """返回 (标准名, 命中的黑话) 列表（黑话完整出现在 query 中）。"""
        hits = []
        for slang, standard in self.mapping.items():
            if slang and slang in query:
                hits.append((standard, slang))
        return hits

    def expand_names(self, names: List[str]) -> List[str]:
        """把黑话词典中的全部标准名并入候选名集合（去重，保证黑话目标可被召回）。"""
        standards = set(self.mapping.values())
        merged = list(dict.fromkeys(list(standards) + list(names)))
        return merged


DEFAULT_BLACK_TALK: Dict[str, str] = {
    # 交易类
    "成交额": "GMV",
    "成交总额": "GMV",
    "销售额": "GMV",
    "营收": "GMV",
    # 用户类
    "日活": "日活跃用户数",
    "日活用户": "日活跃用户数",
    "新增用户": "新增用户数",
    "新客": "新增用户数",
    # 转化类
    "付费率": "付费转化率",
    "转化率": "付费转化率",
    # 客单类
    "客单": "客单价",
    "客单值": "客单价",
}


@dataclass
class CantEntry:
    """维度/维值黑话条目（老 AigcCantName 表行展开后的单条）。"""

    cant_type: str  # "dim" | "dim_value"
    cn_name: str  # 标准名；dim_value 时为 "维度:维值"（K:V 格式）
    cant_cn_name: str  # 黑话


@dataclass
class CantRecallResult:
    """黑话维度/维值召回结果（迁移自老 get_cant_candidates 的 res）。

    - dimensions / dimensions_cant 按序对应（第 i 个维度由第 i 个黑话命中）
    - filters：老格式 [{"dimension", "dimension_value": [...], "filter_operator"}]
    """

    dimensions: List[str] = field(default_factory=list)
    dimensions_cant: List[str] = field(default_factory=list)
    filters: List[Dict[str, Any]] = field(default_factory=list)


class CantDictionary:
    """维度/维值黑话词典（迁移自老 candidates_cant.py::get_cant_candidates）。"""

    def __init__(self, entries: Optional[List[CantEntry]] = None) -> None:
        self.entries = entries or []

    @classmethod
    def from_raw_rows(cls, rows: Iterable[Dict[str, Any]]) -> "CantDictionary":
        """从 HM listBySa 原始行构造词典（迁移老 CantFuncs.get_cant_data 的展开逻辑）。

        - dim_value 行：cn_name 按 dimValue 逗号展开为 "维度:维值"（每个维值一行）
        - cant_cn_name / cn_name 逗号分隔 -> 拆成多行（老 split_and_stack + str.split）
        - cant_cn_name 去除空格（老 get_cant_data 末尾 strip）
        """
        entries: List[CantEntry] = []
        for row in rows or []:
            cant_type = str(row.get("cantType") or row.get("cant_type") or "")
            cn_name = str(row.get("cnName") or row.get("cn_name") or "")
            slang_raw = str(row.get("cantCnName") or row.get("cant_cn_name") or "")
            if not cn_name or not slang_raw:
                continue
            names = [cn_name]
            if cant_type == "dim_value":
                dim_value = str(row.get("dimValue") or row.get("dim_value") or "")
                if dim_value:
                    names = [f"{cn_name}:{v}" for v in dim_value.split(",") if v]
            for slang in (s.replace(" ", "") for s in slang_raw.split(",") if s):
                for name in names:
                    entries.append(
                        CantEntry(cant_type=cant_type, cn_name=name, cant_cn_name=slang)
                    )
        return cls(entries=entries)

    def recall(self, query: str) -> CantRecallResult:
        """黑话完全匹配（黑话字符串出现在 query 中即命中，迁移 get_cant_candidates）。"""
        result = CantRecallResult()
        if not query:
            return result
        for entry in self.entries:
            slang = entry.cant_cn_name
            if not slang or slang not in query:
                continue
            if entry.cant_type == "dim":
                if entry.cn_name:
                    result.dimensions.append(entry.cn_name)
                    result.dimensions_cant.append(slang)
            elif entry.cant_type == "dim_value":
                # cn_name 为 "维度:维值"（K:V），非 K:V 格式的跳过（老 check ":" in cn_name）
                dimension, sep, value = entry.cn_name.partition(":")
                if sep and value:
                    result.filters.append(
                        {
                            "dimension": dimension,
                            "dimension_value": [value],
                            "filter_operator": "in",
                        }
                    )
        return result


DEFAULT_CANT_DICT: List[Dict[str, Any]] = [
    # dim 黑话：用户说"渠道类型" -> 维度"渠道"
    {"cant_type": "dim", "cn_name": "渠道", "cant_cn_name": "渠道类型"},
    {"cant_type": "dim", "cn_name": "地区", "cant_cn_name": "区域"},
    # dim_value 黑话：用户说"大区" -> 过滤 地区 in [华东, 华南]
    {"cant_type": "dim_value", "cn_name": "地区", "dim_value": "华东,华南", "cant_cn_name": "大区"},
]
