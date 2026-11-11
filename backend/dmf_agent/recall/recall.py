"""三路候选召回（full / black_talk / part 融合）。

迁移自老系统 candidates_metric.py 的 get_metric_candidates：
- full       : AC 自动机完全匹配（候选名在 query 中完整出现）
- black_talk : 黑话/俗称完全匹配 -> 标准名（走完全匹配不走 LLM）
- part       : 字符级 Jaccard 部分匹配（错别字/口语容忍，min_score 阈值过滤 + top-N）
- embed      : 向量召回（老系统 HmMetricEmbedding + Milvus），依赖外部向量服务，
               按 MIGRATION.md Phase 5 后置；配置 enable_embedding 预留

融合规则（2026-08-31 优化，迁移老 weight_sort）：
- 分层加权：full/black_talk 默认 1.0、embed 0.6、part 0.4（可配置 DMF_RECALL-WEIGHT_*）
- 同名取加权后最高分来源（matched_by 标注）
- 相对阈值过滤：有强命中时抑制加权后低于"最高分 * ratio"的部分匹配噪声
  （对齐老系统 >= 最高分/3），随后按分数降序稳定排序、截断 max_nums。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from dmf_agent.config.settings import RecallSettings
from dmf_agent.recall.ac_matcher import max_match_words
from dmf_agent.recall.black_talk import BlackTalkDictionary, DEFAULT_BLACK_TALK
from dmf_agent.recall.similarity import jaccards_score_comput


@dataclass
class RecalledItem:
    name: str
    score: float  # 加权融合后的分数
    matched_by: str  # "full" | "black_talk" | "part"


class MetricRecaller:
    def __init__(
        self,
        settings: Optional[RecallSettings] = None,
        black_talk: Optional[BlackTalkDictionary] = None,
    ) -> None:
        self._settings = settings or RecallSettings()
        self._black_talk = black_talk or BlackTalkDictionary(DEFAULT_BLACK_TALK)

    def recall(
        self, query: str, names: List[str], max_nums: Optional[int] = None
    ) -> List[RecalledItem]:
        """对候选名集合做三路召回、分层加权融合、相对阈值过滤并排序。"""
        if not query or not names:
            return []
        limit = max_nums or self._settings.max_nums
        names = list(dict.fromkeys(names))
        # 黑话标准名并入候选，保证黑话目标可被召回
        names = self._black_talk.expand_names(names)

        # 分层权重（对齐老 weight_sort，可配置；同名取加权后最高分来源）
        weights = {
            "full": self._settings.weight_full,
            "black_talk": self._settings.weight_black_talk,
            "part": self._settings.weight_part,
            "embed": self._settings.weight_embed,
        }
        scores: Dict[str, float] = {}
        matched_by: Dict[str, str] = {}

        # 1. 黑话完全匹配（迁移老系统 cant 通道，权重显式生效）
        for standard, _slang in self._black_talk.lookup(query):
            self._merge(scores, matched_by, standard, 1.0 * weights["black_talk"], "black_talk")

        # 2. 完全匹配（AC 自动机）
        for name in max_match_words(query, names):
            self._merge(scores, matched_by, name, 1.0 * weights["full"], "full")

        # 3. 部分匹配（字符级 Jaccard，min_score 已在打分处过滤）
        for name, score in jaccards_score_comput(
            query, names, self._settings.min_score, limit
        ).items():
            self._merge(scores, matched_by, name, score * weights["part"], "part")

        # 融合排序：加权分数降序，分数相同按名称稳定排序
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

        # 相对阈值过滤（迁移老 weight_sort 的 >= 最高分/3）：
        # 有强命中（full/黑话 1.0）时，抑制加权后低于阈值的部分匹配噪声
        if self._settings.enable_relative_threshold and ranked:
            cutoff = ranked[0][1] * self._settings.relative_threshold_ratio
            ranked = [kv for kv in ranked if kv[1] >= cutoff]

        return [
            RecalledItem(name=name, score=score, matched_by=matched_by[name])
            for name, score in ranked[:limit]
        ]

    @staticmethod
    def _merge(
        scores: Dict[str, float],
        matched_by: Dict[str, str],
        name: str,
        score: float,
        source: str,
    ) -> None:
        if score > scores.get(name, 0.0):
            scores[name] = score
            matched_by[name] = source
