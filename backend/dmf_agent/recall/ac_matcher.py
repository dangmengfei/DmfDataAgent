"""AC 自动机完全匹配（候选召回 full 通道）。

迁移自老系统 plugin/utils/AC_recall.py::ACAutomaton 的
maximum_match_word（词库在文本中的最大匹配，剔除被包含的重叠词）。
pyahocorasick 为编译扩展，未安装时退化为朴素子串包含（性能下降、语义一致）。
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

try:
    import ahocorasick  # type: ignore

    _HAS_AC = True
except ImportError:  # pragma: no cover
    ahocorasick = None
    _HAS_AC = False


def _build_automaton(words: List[str]):
    automaton = ahocorasick.Automaton()
    for w in words:
        automaton.add_word(w, w)
    automaton.make_automaton()
    return automaton


def max_match_words(query: str, vocab: List[str]) -> List[str]:
    """返回 vocab 中在 query 里出现、且未被其他词在同一位置包含的词。

    迁移自老系统 maximum_match_word（不含英文数字掩码简化版）：
    先做最大匹配去重（同位置被更长词包含的词剔除），再对剩余词按出现顺序去重。
    """
    if not vocab or not query:
        return []

    if _HAS_AC:
        automaton = _build_automaton(list(dict.fromkeys(vocab)))
        matched = []
        positions = []
        for end_inx, word in automaton.iter(query):
            matched.append(word)
            positions.append([end_inx - len(word) + 1, end_inx + 1])
    else:  # 朴素退化：遍历 vocab 找包含
        matched = []
        positions = []
        lowered = query.lower()
        for word in dict.fromkeys(vocab):
            if not word:
                continue
            start = 0
            while True:
                pos = lowered.find(word.lower(), start)
                if pos == -1:
                    break
                matched.append(word)
                positions.append([pos, pos + len(word)])
                start = pos + 1

    if not matched:
        return []

    # 剔除在原字符串同位置被更长词包含的词（保留最长）
    keep = [True] * len(matched)
    for i in range(len(matched)):
        for j in range(len(matched)):
            if i == j:
                continue
            si, ei = positions[i]
            sj, ej = positions[j]
            overlapped = (sj <= si < ej) or (sj < ei <= ej)
            if overlapped:
                if len(matched[i]) < len(matched[j]):
                    keep[i] = False
                    break
                if len(matched[i]) == len(matched[j]) and i > j:
                    keep[i] = False
                    break

    result = []
    for idx, keep_flag in enumerate(keep):
        if keep_flag and matched[idx] not in result:
            result.append(matched[idx])
    return result
