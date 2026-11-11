"""字符串相似度打分（候选召回用）。

迁移自老系统 get_common_str.py 中**实际参与候选召回**的部分：
- part_jaccard_score / jaccards_score_comput : 字符级 Jaccard 部分匹配打分
（老 get_common_str_by_interval 容错公共子串用于意图兜底 find_max_match，
  不在候选召回链路内，暂不迁移）
"""

from __future__ import annotations

import heapq
from typing import Dict, List

from dmf_agent.recall.segment import split_str_by_cn


def part_jaccard_score(
    query: str, shots: List[str], min_value: float, max_num: int
) -> Dict[str, float]:
    """字符级 Jaccard 部分匹配打分：score = |query∩shot| / |shot|。

    迁移自老系统同名函数：命中率以**候选词长度**为分母（而非并集），
    更适合"候选名被 query 部分包含"的召回场景；用小顶堆保留 top-N。
    """
    query_set = set(split_str_by_cn(query))
    heap: List[tuple] = []
    for shot in shots:
        shot_set = set(split_str_by_cn(shot))
        score = 0.0 if not shot_set else 1.0 * len(query_set & shot_set) / len(shot_set)
        if score >= min_value:
            heapq.heappush(heap, (score, shot))
            if len(heap) > max_num:
                heapq.heappop(heap)
    return {item[1]: item[0] for item in heapq.nlargest(max_num, heap)}


def jaccards_score_comput(
    query, shots_list: List[str], min_value: float, max_num: int
) -> Dict[str, float]:
    """对单个 query（str）或多个 query（list）批量打分。"""
    if isinstance(query, str):
        return part_jaccard_score(query, shots_list, min_value, max_num)
    result: Dict[str, float] = {}
    for text in query:
        result.update(part_jaccard_score(text, shots_list, min_value, max_num))
    return result
