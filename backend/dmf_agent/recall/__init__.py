"""候选集召回模块（Phase 2.1）。

迁移自老系统 get_metric_candidates 的三路召回：
- segment.py    : 字符级分词（中文逐字、英文/数字连续）——老 get_common_str.py::split_str_by_cn
- similarity.py : Jaccard 部分匹配打分 / 容错公共子串 —— 老 get_common_str.py 的打分部分
- ac_matcher.py : AC 自动机完全匹配 —— 老 AC_recall.py::ACAutomaton
- black_talk.py : 黑话词典（老系统 MySQL cant 表，v2 配置驱动）
- recall.py     : MetricRecaller 三路融合（老 candidates_metric.py 的 full/part/embed 合并）
- common_str.py : 容错公共子序列（老 get_common_str_by_interval，维度/过滤打分用）
- dimension.py  : DimensionRecaller 维度召回（老 candidates_dim.py 的 get_dim_score）
- filter.py     : FilterRecaller 过滤召回（老 candidates_filter.py 的 get_filter_score，
  数据源 HM dim/getByValue 见 metadata/client.py::fetch_dim_values）
- black_talk.py : CantDictionary 维度/维值黑话（老 candidates_cant.py，cantType=dim/dim_value，
  数据源 HM listBySa 见 metadata/client.py::fetch_cants）

向量召回（embedding）依赖外部向量服务，按 MIGRATION.md 后置（Phase 5）。
"""

from dmf_agent.recall.black_talk import (
    BlackTalkDictionary,
    CantDictionary,
    CantEntry,
    CantRecallResult,
    DEFAULT_BLACK_TALK,
    DEFAULT_CANT_DICT,
)
from dmf_agent.recall.common_str import get_common_str_by_interval
from dmf_agent.recall.dimension import (
    DimensionRecaller,
    DimensionRecallResult,
    get_primary_name,
    is_substr,
)
from dmf_agent.recall.filter import (
    FilterCandidate,
    FilterRecaller,
    FilterRecallResult,
)
from dmf_agent.recall.recall import MetricRecaller, RecalledItem

__all__ = [
    "BlackTalkDictionary",
    "CantDictionary",
    "CantEntry",
    "CantRecallResult",
    "DEFAULT_BLACK_TALK",
    "DEFAULT_CANT_DICT",
    "DimensionRecaller",
    "DimensionRecallResult",
    "FilterCandidate",
    "FilterRecaller",
    "FilterRecallResult",
    "MetricRecaller",
    "RecalledItem",
    "get_common_str_by_interval",
    "get_primary_name",
    "is_substr",
]
